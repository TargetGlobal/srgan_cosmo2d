__author__ = "Jan Balewski"
__email__ = "janstar1122@gmail.com"

'''
this data loader reads one shard of data from a common h5-file upon start, there is no distributed sampler!!

reads all data at once and serves them from RAM
- optimized for mult-GPU training
- only used block of data  from each H5-file
- reads data from common file for all ranks
- allows for in-fly transformation

Shuffle: only local samples after read is compleated

Dataloader expects 2D the HR-images stack one another and split into train/val/test domains.
E.g. small file:
/global/cscratch1/sd/balewski/srgan_cosmo2d_data/univL9cola_dm2d_202204_c30.h5
h5-write : val.hr (4608, 512, 512, 2) uint8
h5-write : test.hr (4608, 512, 512, 2) uint8
h5-write : train.hr (36864, 512, 512, 2) uint8
The last dimR 0: zRed=50=ini, 1:zRed=0.=fin

Typical input will be sampled along the 1st axis,
randomized by mirroring and/or 90-rot,
the lr.fin-image is derived from  hr.fin by downsampling

'''

import time,  os
import random
import h5py
import numpy as np
import json
from pprint import pprint

import copy
from torch.utils.data import Dataset, DataLoader
import torch 
import logging
from toolbox.Util_IOfunc import read_yaml
from toolbox.Util_Cosmo2d import   random_flip_rot_WHC
from toolbox.Util_Torch import transf_field2img_torch

#...!...!..................
def get_data_loader(trainMD,domain, verb=1):
  trainMD['data_shape']={'upscale_factor': (1<<trainMD['model_conf']['G']['num_upsamp_bits'])}
  conf=copy.deepcopy(trainMD)  # the input may be reused later in the upper level code
  
  dataset=  Dataset_h5_srgan2D(conf,domain,verb)
  
  # return back some info
  trainMD[domain+'_steps_per_epoch']=dataset.sanity()
  for x in ['data_shape']: #1,'sim3d','field2d']:
      trainMD[x]=conf[x]

  # data dimension is know after data are read in
  cfds=conf['data_shape']
  trainMD['data_shape']['hr_img']=[conf['num_inp_chan'],cfds['hr_size'],cfds['hr_size']]
  trainMD['data_shape']['lr_img']=[conf['num_inp_chan'],cfds['lr_size'],cfds['lr_size']]
  dataloader = DataLoader(dataset,
                          batch_size=conf['local_batch_size'],
                          num_workers=conf['num_cpu_workers'],
                          shuffle=conf['shuffle'],
                          drop_last=True,
                          pin_memory=torch.cuda.is_available())

  return dataloader

#-------------------
#-------------------
#-------------------
class Dataset_h5_srgan2D(object):
#...!...!..................    
    def __init__(self, conf,domain,verb=1):
        conf['domain']=domain
       
        fieldN=conf['data_conf']['field_name']
        zIni=conf['data_conf']['zred_ini']
        zFin=conf['data_conf']['zred_fin']
        
        conf['rec_hrIni']='%s_%s_HR_%s'%(domain,fieldN,zIni)
        conf['rec_lrFin']='%s_%s_LR_%s'%(domain,fieldN,zFin)
        conf['rec_hrFin']='%s_%s_HR_%s'%(domain,fieldN,zFin)
        assert conf['world_rank']>=0
        
        self.conf=conf
        self.verb=verb

        self.openH5() # computes final dimensionality of data
        assert self.numLocalSamp>0

        if self.verb :
            logging.info(' DS:load-end %s locSamp=%d'%(self.conf['domain'],self.numLocalSamp))
            #print(' DS:Xall',self.data_frames.shape,self.data_frames.dtype)
            #print(' DS:Yall',self.data_parU.shape,self.data_parU.dtype)
            

#...!...!..................
    def sanity(self):      
        stepPerEpoch=int(np.floor( self.numLocalSamp/ self.conf['local_batch_size']))
        if  stepPerEpoch <1:
            logging.error('DLI: Have you requested too few samples per rank?, numLocalSamp=%d, BS=%d  dom=%s'%(self.numLocalSamp, self.conf['local_batch_size'],self.conf['domain']))
            exit(67)
        # all looks good
        return stepPerEpoch
        
#...!...!..................
    def openH5(self):
        cf=self.conf
        inpF=os.path.join(cf['h5_path'],cf['h5_name'])
        dom=cf['domain']
        if self.verb>0 : logging.info('DS:fileH5 %s  rank %d of %d '%(inpF,cf['world_rank'],cf['world_size']))
        
        if not os.path.exists(inpF):
            print('DLI:FAILED, missing HD5',inpF)
            exit(22)

        startTm0 = time.time()
                
        # = = = READING HD5  start
        h5f = h5py.File(inpF, 'r')
        metaJ=h5f['meta.JSON'][0]
        inpMD=json.loads(metaJ)
        #inpMD['packing']={'raw_cube_shape':[4096,4096,4096]} # tmp

        
        cfds=cf['data_shape']
        #1print('DL:inpD'); pprint(inpMD); ok45
        #?cfds['hr_size']=inpMD['packing']['raw_cube_shape'][0]
        cfds['hr_size']=h5f[cf['rec_hrIni']].shape[1]
        cfds['lr_size']=h5f[cf['rec_lrFin']].shape[1]
        
        assert cfds['hr_size'] ==cfds['upscale_factor']*cfds['lr_size']
        
        #print('DL:recovered meta-data with %d keys dom=%s'%(len(inpMD),dom))
        #1cf.update(prep_fieldMD(inpMD,cf))
        #print('DL:cfD'); pprint(cf)
       
        totSamp=h5f[cf['rec_hrIni']].shape[0]
        
        if 'max_glob_samples_per_epoch' in cf:            
            max_samp= cf['max_glob_samples_per_epoch']
            if dom=='valid': max_samp//=4
            oldN=totSamp
            totSamp=min(totSamp,max_samp)
            if totSamp<oldN and  self.verb>0 :
              logging.warning('GDL: shorter dom=%s max_glob_samples=%d from %d'%(dom,totSamp,oldN))            
            #idxRange[1]=idxRange[0]+totSamp

        
        self.inpMD=inpMD # will be needed later too        
        locStep=int(totSamp/cf['world_size']/cf['local_batch_size'])
        locSamp=locStep*cf['local_batch_size']
        if  self.verb>0 :
          logging.info('DLI:globSamp=%d locStep=%d BS=%d dom=%s'%(totSamp,locStep,cf['local_batch_size'],dom))
        assert locStep>0
        maxShard= totSamp// locSamp
        assert maxShard>=cf['world_size']
                    
        # chosen shard is rank dependent, wraps up if not sufficient number of ranks
        myShard=self.conf['world_rank'] %maxShard
        sampIdxOff=myShard*locSamp
        
        if self.verb: logging.info('DS:file dom=%s myShard=%d, maxShard=%d, sampIdxOff=%d '%(dom,myShard,maxShard,sampIdxOff))       
        
        # data reading starts ....
        self.data_hrIni=h5f[ cf['rec_hrIni']][sampIdxOff:sampIdxOff+locSamp]
        self.data_lrFin=h5f[ cf['rec_lrFin']][sampIdxOff:sampIdxOff+locSamp]
        self.data_hrFin=h5f[ cf['rec_hrFin']][sampIdxOff:sampIdxOff+locSamp]
        h5f.close()
        # = = = READING HD5  done
        
        if self.verb>0 :
            startTm1 = time.time()
            logging.info('DS: hd5 read time=%.2f(sec) dom=%s data_hrIni.shape=%s dtype=%s'%(startTm1 - startTm0,dom,str(self.data_hrIni.shape),self.data_hrIni.dtype))
            logging.info('DS: hd5  dom=%s data_lrFin.shape=%s data_hrFin.shape=%s'%(dom,str(self.data_lrFin.shape),str(self.data_hrFin.shape)))
            
        # .......................................................
        #.... data embeddings, transformation should go here ....
        #  rescale LR data to have the same integral as HR
        fact=cf['data_shape']['upscale_factor']**2
        self.data_lrFin*=fact
        
        
        #.... end of embeddings ........
        # .......................................................
        self.numLocalSamp=self.data_hrIni.shape[0]
        
        if 0 : # check X normalizations - from neuron inverter, never used        
            X=self.data_frames
            xm=np.mean(X,axis=1)  # average over 1600 time bins
            xs=np.std(X,axis=1)
            print('DLI:X',X[0,::80,0],X.shape,xm.shape)

            print('DLI:Xm',xm[:10],'\nXs:',xs[:10],myShard,'dom=',cf['domain'],'X:',X.shape)
                    

#...!...!..................
    def __len__(self):        
        return self.numLocalSamp

#...!...!..................
    def __getitem__(self, idx):
        cf=self.conf
        cfds=cf['data_shape']
        #print('DSI:idx=',idx,cf['domain'],'rank=',cf['world_rank'])
        assert idx>=0
        assert idx< self.numLocalSamp

        # primary input dtype=uint8 - this is pair of 3D HR densities for initial & final state
        hrIni=self.data_hrIni[idx]
        lrFin=self.data_lrFin[idx]
        hrFin=self.data_hrFin[idx]  
        #print('DSI:hrIni=',hrIni.shape,hrIni.dtype)
        
       
        if cf['image_flip_rot']:
          rndV=np.random.uniform(size=3)
          #print('rrr',rndV); ok99
          hrIni=random_flip_rot_WHC(hrIni,rndV)
          lrFin=random_flip_rot_WHC(lrFin,rndV)
          hrFin=random_flip_rot_WHC(hrFin,rndV)

          
        # use only one chan, convert WH to CWH
        hrIni=hrIni.reshape(1,cfds['hr_size'],-1)
        lrFin=lrFin.reshape(1,cfds['lr_size'],-1)
        hrFin=hrFin.reshape(1,cfds['hr_size'],-1)
        
        #print('DL shape  X',hrIni.shape,lrFin.shape,'Y:',hrFin.shape)
        # transform field to image,computed as log(1+rho)
        hrIniImg=transf_field2img_torch(torch.from_numpy(np.copy(hrIni+1. )) )
        lrFinImg=transf_field2img_torch(torch.from_numpy(np.copy(lrFin+1. )) )
        hrFinImg=transf_field2img_torch(torch.from_numpy(np.copy(hrFin+1. )) )
        
        # fp32-prec data 
        return hrIniImg.float(),lrFinImg.float(),hrFinImg.float()
    
        # finally cast output to fp16 - Jan could not make model to work with it
        #return lrFinImg.half(),hrIniImg.half(),hrFinImg.half()
        # RuntimeError: Input type (torch.cuda.HalfTensor) and weight type (torch.cuda.FloatTensor) should be the same
