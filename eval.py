# System libs
import os
import time
import argparse
from distutils.version import LooseVersion
# Numerical libs
import numpy as np
import torch
import torch.nn as nn
from scipy.io import loadmat
# Our libs
from dataset import ValDataset
from models import ModelBuilder, SegmentationModule
from utils import AverageMeter, colorEncode, accuracy, intersectionAndUnion
from lib.nn import user_scattered_collate, async_copy_to
from lib.utils import as_numpy
import lib.utils.data as torchdata
import cv2
from tqdm import tqdm

from PIL import Image
#colors = loadmat('data/color150.mat')['colors']


def visualize_result(data, pred, args):
    (img, seg, info) = data

    # segmentation
    #seg_color = colorEncode(seg.astype(np.uint8), [args.num_class,3])

    # prediction
    #pred_color = colorEncode(pred.astype(np.uint8), [args.num_class,3])
    
    # aggregate images and save
    #im_vis = np.concatenate((img, seg_color, pred_color),
    #                       axis=1).astype(np.uint8)
    
    pred_img = (pred * 128) - (pred == 2).astype(np.uint8)
    seg_img = (seg * 128) - (seg == 2).astype(np.uint8)
    im_vis = np.concatenate((img, seg_img, pred_img), axis=1).astype(np.uint8)
    img_name = info.split('/')[-1]
    cv2.imwrite(os.path.join(args.result,
                img_name.replace('.jpg', '.png')), im_vis)


def evaluate(segmentation_module, loader, args):
    acc_meter = AverageMeter()
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    time_meter = AverageMeter()

    segmentation_module.eval()

    pbar = tqdm(total=len(loader))
    for batch_data in loader:
        # process data
        batch_data = batch_data[0]
        #print(batch_data[0])
        seg_label = as_numpy(batch_data['seg_label'])
        img_resized_list = batch_data['img_data'].unsqueeze(0)
        #print(seg_label.shape)
        #print(img_resized_list.shape)
        '''
        # fix the dimension permutation
        for idx, item in enumerate(batch_data['img_data']):
            print(item.shape)
            batch_data['img_data'][idx] = item.permute(0, 3, 1, 2)

        print()
            
        for item in batch_data['img_data']:
            print(item.shape)
        '''

        torch.cuda.synchronize()
        tic = time.perf_counter()
        with torch.no_grad():
            segSize = (seg_label.shape[0], seg_label.shape[1])
            scores = torch.zeros(1, args.num_class, segSize[0], segSize[1])
            scores = async_copy_to(scores, args.gpu)

            for img in img_resized_list:
                feed_dict = batch_data.copy()
                feed_dict['img_data'] = img
                del feed_dict['img_ori']
                del feed_dict['info']
                feed_dict = async_copy_to(feed_dict, args.gpu)

                # forward pass
                scores_tmp = segmentation_module(feed_dict, segSize=segSize)
                scores = scores + scores_tmp / len(args.imgSize)

            _, pred = torch.max(scores, dim=1)

            pred = as_numpy(pred.squeeze(0).cpu())

        torch.cuda.synchronize()
        time_meter.update(time.perf_counter() - tic)

        # calculate accuracy
        acc, pix = accuracy(pred, seg_label)
        intersection, union = intersectionAndUnion(pred, seg_label, args.num_class)
        acc_meter.update(acc, pix)
        intersection_meter.update(intersection)
        union_meter.update(union)

        # visualization
        if True:# args.visualize
            visualize_result(
                (batch_data['img_ori'], seg_label, batch_data['info']),
                pred, args)

        pbar.update(1)

    # summary
    iou = intersection_meter.sum / (union_meter.sum + 1e-10)
    for i, _iou in enumerate(iou):
        print('class [{}], IoU: {:.4f}'.format(i, _iou))

    print('[Eval Summary]:')
    print('Mean IoU: {:.4f}, Accuracy: {:.2f}%, Inference Time: {:.4f}s'
          .format(iou.mean(), acc_meter.average()*100, time_meter.average()))


def main(args):
    torch.cuda.set_device(args.gpu)

    # Network Builders
    builder = ModelBuilder()
    
    net_encoder = None
    net_decoder = None
    unet = None

    if args.unet == False:
        net_encoder = builder.build_encoder(
            arch=args.arch_encoder,
            fc_dim=args.fc_dim,
            weights=args.weights_encoder)
        net_decoder = builder.build_decoder(
            arch=args.arch_decoder,
            fc_dim=args.fc_dim,
            num_class=args.num_class,
            weights=args.weights_decoder,
            use_softmax=True)
    else:
        unet = builder.build_unet(num_class=args.num_class,
            arch=args.arch_unet,
            weights=args.weights_unet,
            use_softmax=True)

    crit = nn.NLLLoss()
    
    if args.unet == False:
        segmentation_module = SegmentationModule(net_encoder, net_decoder, crit)
    else:
        segmentation_module = SegmentationModule(net_encoder, net_decoder, crit,
                                                is_unet=args.unet, unet=unet)
    '''
    # Dataset and Loader
    dataset_val = dl.loadVal()

    loader_val = torchdata.DataLoader(
        dataset_val,
        batch_size=5,
        shuffle=False,
        num_workers=1,
        drop_last=True)
    '''

    # Dataset and Loader
    dataset_val = ValDataset(
        args.list_val, args, max_sample=args.num_val)

    loader_val = torchdata.DataLoader(
        dataset_val,
        batch_size=args.batch_size,
        shuffle=False,
        collate_fn=user_scattered_collate,
        num_workers=5,
        drop_last=True)

    segmentation_module.cuda()

    # Main loop
    evaluate(segmentation_module, loader_val, args)

    print('Evaluation Done!')


if __name__ == '__main__':
    assert LooseVersion(torch.__version__) >= LooseVersion('0.4.0'), \
        'PyTorch>=0.4.0 is required'

    parser = argparse.ArgumentParser()
    # Model related arguments
    parser.add_argument('--id', required=True,
                        help="a name for identifying the model to load")
    parser.add_argument('--suffix', default='_epoch_20.pth',
                        help="which snapshot to load")
    parser.add_argument('--arch_encoder', default='resnet50dilated',
                        help="architecture of net_encoder")
    parser.add_argument('--arch_decoder', default='ppm_deepsup',
                        help="architecture of net_decoder")
    parser.add_argument('--fc_dim', default=2048, type=int,
                        help='number of features between encoder and decoder')
    parser.add_argument('--unet', default=True,
                        help='Use a UNet?')
    parser.add_argument('--arch_unet', default='albunet',
                        help='UNet architecture?')

    # Path related arguments
    parser.add_argument('--list_val',
                        default='/home/rexma/Desktop/seg/data/validation.odgt')
    parser.add_argument('--root_dataset',
                        default='/home/rexma/Desktop/seg/data/')

    # Data related arguments
    parser.add_argument('--num_val', default=-1, type=int,
                        help='number of images to evalutate')
    parser.add_argument('--num_class', default=3, type=int,
                        help='number of classes')
    parser.add_argument('--batch_size', default=1, type=int,
                        help='batchsize. current only supports 1')
    parser.add_argument('--imgSize', default=[127,83,97,130,165,118,142,384,256,528,150,95,140,170], nargs='+', type=int,
                        help='list of input image sizes.'
                             'for multiscale testing, e.g.  300 400 500 600')
    parser.add_argument('--imgMaxSize', default=528, type=int,
                        help='maximum input image size of long edge')
    parser.add_argument('--padding_constant', default=1, type=int,
                        help='maxmimum downsampling rate of the network')

    # Misc arguments
    parser.add_argument('--ckpt', default='/home/rexma/Desktop/seg/ckpt',
                        help='folder to output checkpoints')
    parser.add_argument('--visualize', action='store_true',
                        help='output visualization?')
    parser.add_argument('--result', default='/home/rexma/Desktop/seg/result',
                        help='folder to output visualization results')
    parser.add_argument('--gpu', default=0, type=int,
                        help='gpu id for evaluation')

    args = parser.parse_args()
    args.arch_encoder = args.arch_encoder.lower()
    args.arch_decoder = args.arch_decoder.lower()
    print("Input arguments:")
    for key, val in vars(args).items():
        print("{:16} {}".format(key, val))

    # absolute paths of model weights
    if args.unet == False:
        args.weights_encoder = os.path.join(args.ckpt, args.id,
                                            'encoder' + args.suffix)
        args.weights_decoder = os.path.join(args.ckpt, args.id,
                                            'decoder' + args.suffix)
    
        assert os.path.exists(args.weights_encoder) and \
            os.path.exists(args.weights_encoder), 'checkpoint does not exitst!'
    
    else:
        args.weights_unet = os.path.join(args.ckpt, args.id,
                                        'unet' + args.suffix)
        
        assert os.path.exists(args.weights_unet), 'checkpoint does not exist!'

    args.result = os.path.join(args.result, args.id)
    if not os.path.isdir(args.result):
        os.makedirs(args.result)

    main(args)
