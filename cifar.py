# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Main script to launch AugMix training on CIFAR-10/100.

Supports WideResNet, AllConv, ResNeXt models on CIFAR-10 and CIFAR-100 as well
as evaluation on CIFAR-10-C and CIFAR-100-C.

Example usage:
  `python cifar.py`
"""
from __future__ import print_function

import argparse
import os
import shutil
import time

import augmentations
from models.cifar.allconv import AllConvNet
import numpy as np
from torch.nn import Linear
from third_party.ResNeXt_DenseNet.models.densenet import densenet
from third_party.ResNeXt_DenseNet.models.resnext import resnext29
from third_party.WideResNet_pytorch.wideresnet import WideResNet

import torch
import torchvision
import timm
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
from torchvision import datasets
from torchvision import transforms
from torchvision import models
from torchvision.models import resnet18

 

from torch.utils.tensorboard import SummaryWriter
writer  = SummaryWriter("runs/cifar")


parser = argparse.ArgumentParser(
    description='Trains a CIFAR Classifier',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument(
    '--dataset',
    type=str,
    default='cifar10',
    choices=['cifar10', 'cifar100'],
    help='Choose between CIFAR-10, CIFAR-100.')
parser.add_argument(
    '--model',
    '-m',
    type=str,
    default='wrn',
    choices=['wrn', 'allconv', 'densenet', 'resnext','convnext_tiny','resnet18','resnet18_PT','convnext_tiny_PT'],
    help='Choose architecture.')
# Optimization options

parser.add_argument(
  '--optimizer',
  '-o',
  type=str,
  default='sgd',
  choices=['sgd','adamW'],
  help='choose optimizer')
parser.add_argument(
  '--scheduler',
  '-s',
  type=str,
  default='lambda',
  choices=['lambda','cosineannealing'],
  help='Choose learning rate scheduler')
parser.add_argument(
    '--epochs', '-e', type=int, default=100, help='Number of epochs to train.')
parser.add_argument(
    '--learning-rate',
    '-lr',
    type=float,
    default=0.1,
    help='Initial learning rate.')
parser.add_argument(
    '--batch-size', '-b', type=int, default=128, help='Batch size.')
parser.add_argument('--eval-batch-size', type=int, default=1000)
parser.add_argument('--momentum', type=float, default=0.9, help='Momentum.')
parser.add_argument(
    '--decay',
    '-wd',
    type=float,
    default=0.0005,
    help='Weight decay (L2 penalty).')
# WRN Architecture options
parser.add_argument(
    '--layers', default=40, type=int, help='total number of layers')
parser.add_argument('--widen-factor', default=2, type=int, help='Widen factor')
parser.add_argument(
    '--droprate', default=0.0, type=float, help='Dropout probability')
# AugMix options
parser.add_argument(
    '--mixture-width',
    default=3,
    type=int,
    help='Number of augmentation chains to mix per augmented example')
parser.add_argument(
    '--mixture-depth',
    default=-1,
    type=int,
    help='Depth of augmentation chains. -1 denotes stochastic depth in [1, 3]')
parser.add_argument(
    '--aug-severity',
    default=3,
    type=int,
    help='Severity of base augmentation operators')
parser.add_argument(
    '--no-jsd',
    '-nj',
    action='store_true',
    help='Turn off JSD consistency loss.')
parser.add_argument(
    '--all-ops',
    '-all',
    action='store_true',
    help='Turn on all operations (+brightness,contrast,color,sharpness).')
# Checkpointing options
parser.add_argument(
    '--save',
    '-S',
    type=str,
    default='./snapshots',
    help='Folder to save checkpoints.')
parser.add_argument(
    '--resume',
    '-r',
    type=str,
    default='',
    help='Checkpoint path for resume / test.')
parser.add_argument('--evaluate', action='store_true', help='Eval only.')
parser.add_argument(
    '--print-freq',
    type=int,
    default=50,
    help='Training loss print frequency (batches).')
# Acceleration
parser.add_argument(
    '--num-workers',
    type=int,
    default=4,
    help='Number of pre-fetching threads.')

args = parser.parse_args()

CORRUPTIONS = [
    'gaussian_noise', 'shot_noise', 'impulse_noise', 'defocus_blur',
    'glass_blur', 'motion_blur', 'zoom_blur', 'snow', 'frost', 'fog',
    'brightness', 'contrast', 'elastic_transform', 'pixelate',
    'jpeg_compression'
]

PERTURBATIONS = [
    'brightness', 'gaussian_blur', 'gaussian_noise_2', 'gaussian_noise_3','gaussian_noise',
    'motion_blur', 'rotate', 'scale', 'shear', 'shot_noise_2','shot_noise_3','shot_noise',
    'snow', 'spatter', 'speckle_noise_2', 'speckle_noise_3','speckle_noise','tilt',
    'translate','zoom_blur'
]



def get_lr(step, total_steps, lr_max, lr_min):
  """Compute learning rate according to cosine annealing schedule."""
  return lr_min + (lr_max - lr_min) * 0.5 * (1 +
                                             np.cos(step / total_steps * np.pi))


def aug(image, preprocess):
  """Perform AugMix augmentations and compute mixture.

  Args:
    image: PIL.Image input image
    preprocess: Preprocessing function which should return a torch tensor.

  Returns:
    mixed: Augmented and mixed image.
  """
  aug_list = augmentations.augmentations
  if args.all_ops:
    aug_list = augmentations.augmentations_all

  ws = np.float32(np.random.dirichlet([1] * args.mixture_width))
  m = np.float32(np.random.beta(1, 1))

  mix = torch.zeros_like(preprocess(image))
  for i in range(args.mixture_width):
    image_aug = image.copy()
    depth = args.mixture_depth if args.mixture_depth > 0 else np.random.randint(
        1, 4)
    for _ in range(depth):
      op = np.random.choice(aug_list)
      image_aug = op(image_aug, args.aug_severity)
    # Preprocessing commutes since all coefficients are convex
    mix += ws[i] * preprocess(image_aug)

  mixed = (1 - m) * preprocess(image) + m * mix
  return mixed


class AugMixDataset(torch.utils.data.Dataset):
  """Dataset wrapper to perform AugMix augmentation."""

  def __init__(self, dataset, preprocess, no_jsd=False):
    self.dataset = dataset
    self.preprocess = preprocess
    self.no_jsd = no_jsd

  def __getitem__(self, i):
    x, y = self.dataset[i]
    if self.no_jsd:
      return aug(x, self.preprocess), y
    else:
      im_tuple = (self.preprocess(x), aug(x, self.preprocess),
                  aug(x, self.preprocess))
      return im_tuple, y

  def __len__(self):
    return len(self.dataset)


def train(net, train_loader, optimizer, scheduler):
  """Train for one epoch."""
  net.train()
  loss_ema = 0.
  for i, (images, targets) in enumerate(train_loader):
    optimizer.zero_grad()

    if args.no_jsd:
      images = images.cuda()
      targets = targets.cuda()
      logits = net(images)
      loss = F.cross_entropy(logits, targets)
    else:
      images_all = torch.cat(images, 0).cuda()
      targets = targets.cuda()
      logits_all = net(images_all)
      logits_clean, logits_aug1, logits_aug2 = torch.split(
          logits_all, images[0].size(0))

      # Cross-entropy is only computed on clean images
      loss = F.cross_entropy(logits_clean, targets)

      p_clean, p_aug1, p_aug2 = F.softmax(
          logits_clean, dim=1), F.softmax(
              logits_aug1, dim=1), F.softmax(
                  logits_aug2, dim=1)

      # Clamp mixture distribution to avoid exploding KL divergence
      p_mixture = torch.clamp((p_clean + p_aug1 + p_aug2) / 3., 1e-7, 1).log()
      loss += 12 * (F.kl_div(p_mixture, p_clean, reduction='batchmean') +
                    F.kl_div(p_mixture, p_aug1, reduction='batchmean') +
                    F.kl_div(p_mixture, p_aug2, reduction='batchmean')) / 3.

    loss.backward()
    optimizer.step()
    scheduler.step()
    loss_ema = loss_ema * 0.9 + float(loss) * 0.1
    if i % args.print_freq == 0:
      print('Train Loss {:.3f}'.format(loss_ema))
      
  return loss_ema


def test(net, test_loader):
  """Evaluate network on given dataset."""
  net.eval()
  total_loss = 0.
  total_correct = 0
  with torch.no_grad():
    for images, targets in test_loader:
      images, targets = images.cuda(), targets.cuda()
      logits = net(images)
      loss = F.cross_entropy(logits, targets)
      pred = logits.data.max(1)[1]
      total_loss += float(loss.data)
      total_correct += pred.eq(targets.data).sum().item()

  return total_loss / len(test_loader.dataset), total_correct / len(
      test_loader.dataset)


def test_c(net, test_data, base_path):
  """Evaluate network on given corrupted dataset."""
  corruption_accs = []
  for corruption in CORRUPTIONS:
    # Reference to original data is mutated
    test_data.data = np.load(base_path + corruption + '.npy')
    test_data.targets = torch.LongTensor(np.load(base_path + 'labels.npy'))

    test_loader = torch.utils.data.DataLoader(
        test_data,
        batch_size=args.eval_batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True)

    test_loss, test_acc = test(net, test_loader)
    corruption_accs.append(test_acc)
    print('{}\n\tTest Loss {:.3f} | Test Error {:.3f}'.format(
        corruption, test_loss, 100 - 100. * test_acc))

  return np.mean(corruption_accs)

"""Robustness(https://github.com/hendrycks/robustness/blob/master/ImageNet-P/cifar-p-eval.py)."""

def flip_prob(predictions, noise_perturbation=False):
    result = 0
    step_size = 1
    for vid_preds in predictions:
        result_for_vid = []

        for i in range(step_size):
            prev_pred = vid_preds[i]

            for pred in vid_preds[i::step_size][1:]:
                result_for_vid.append(int(prev_pred != pred))
                if not noise_perturbation: prev_pred = pred

        result += np.mean(result_for_vid) / len(predictions)
    return result

"""Robustness(https://github.com/hendrycks/robustness/blob/master/ImageNet-P/cifar-p-eval.py)."""

def test_p(net, base_path,num_classes,tensorboard_summaryWriter=None):
  """Evaluate network on given perturbated dataset."""
  flip_list = []
  for perturbation in PERTURBATIONS:
    dataset = torch.from_numpy(np.float32(np.load(base_path + perturbation +'.npy').transpose((0,1,4,2,3))))/255.
    loader = torch.utils.data.DataLoader(
        dataset, batch_size=args.eval_batch_size, shuffle=False, num_workers=args.num_workers, pin_memory=True)

    predictions = []
    with torch.no_grad():
      for data in loader:
          num_vids = data.size(0)
          data = data.view(-1,3,32,32).cuda()

          output = net(data * 2 - 1)

          for vid in output.view(num_vids, -1, num_classes):
              predictions.append(vid.argmax(1).to('cpu').numpy())
      current_flip = flip_prob(predictions, True if 'noise' in perturbation else False)
      flip_list.append(current_flip)
      print('\n' + perturbation, 'Flipping Rate')
      print(current_flip)
    # log to Tensorboard
    if(tensorboard_summaryWriter):
      tensorboard_summaryWriter.add_scalars(perturbation + '_p',{'flipping_rate':current_flip},global_step=1)
    
  return np.mean(flip_list)


def main():
  torch.manual_seed(1)
  np.random.seed(1)

  # Load datasets
  train_transform = transforms.Compose(
      [transforms.RandomHorizontalFlip(),
       transforms.RandomCrop(32, padding=4)])
  preprocess = transforms.Compose(
      [transforms.ToTensor(),
       transforms.Normalize([0.5] * 3, [0.5] * 3)])
  test_transform = preprocess

  if args.dataset == 'cifar10':
    train_data = datasets.CIFAR10(
        './data/cifar', train=True, transform=train_transform, download=True)
    test_data = datasets.CIFAR10(
        './data/cifar', train=False, transform=test_transform, download=True)
    base_c_path = './data/cifar/CIFAR-10-C/'
    base_p_path = './data/cifar/CIFAR-10-P/'
    num_classes = 10
  else:
    train_data = datasets.CIFAR100(
        './data/cifar', train=True, transform=train_transform, download=True)
    test_data = datasets.CIFAR100(
        './data/cifar', train=False, transform=test_transform, download=True)
    base_c_path = './data/cifar/CIFAR-100-C/'
    num_classes = 100

  train_data = AugMixDataset(train_data, preprocess, args.no_jsd)
  train_loader = torch.utils.data.DataLoader(
      train_data,
      batch_size=args.batch_size,
      shuffle=True,
      num_workers=args.num_workers,
      pin_memory=True)

  test_loader = torch.utils.data.DataLoader(
      test_data,
      batch_size=args.eval_batch_size,
      shuffle=False,
      num_workers=args.num_workers,
      pin_memory=True)

  # Create model
  if args.model == 'densenet':
    net = densenet(num_classes=num_classes)
  elif args.model == 'wrn':
    net = WideResNet(args.layers, num_classes, args.widen_factor, args.droprate)
  elif args.model == 'allconv':
    net = AllConvNet(num_classes)
  elif args.model == 'resnext':
    net = resnext29(num_classes=num_classes)
  elif args.model == 'resnet18':
    net = resnet18(num_classes=num_classes)
  elif args.model == 'resnet18_PT':
    net = resnet18(pretrained=True)
    num_fea = net.fc.in_features # to get all the input features of last fully connected layer
    net.fc= Linear(in_features=num_fea, out_features=num_classes, bias=True) #creating a new layer and assign it to last layer
  elif args.model == 'convnext_tiny':
    net = timm.create_model("convnext_tiny" , num_classes=num_classes)
  elif args.model == 'convnext_tiny_PT':
    net = timm.create_model("convnext_tiny", pretrained=True)
    inp_fea = net.get_classifier().in_features;  #sequnetial layer classifier
    net.fc = Linear(inp_fea, num_classes)


  #Optimizer  

  if args.optimizer == 'sgd':
    optimizer = torch.optim.SGD(
        net.parameters(),
        args.learning_rate,
        momentum=args.momentum,
        weight_decay=args.decay,
        nesterov=True)
  elif args.optimizer == 'adamW':
    optimizer = torch.optim.AdamW(
        net.parameters(),
        args.learning_rate)

  
  '''optimizer = torch.optim.SGD(  # By default Optimizer
      net.parameters(),
      args.learning_rate,
      momentum=args.momentum,
      weight_decay=args.decay,
      nesterov=True)'''

  # Distribute model across all visible GPUs
  net = torch.nn.DataParallel(net).cuda()
  cudnn.benchmark = True

  start_epoch = 0

  if args.resume:
    if os.path.isfile(args.resume):
      checkpoint = torch.load(args.resume)
      start_epoch = checkpoint['epoch'] + 1
      best_acc = checkpoint['best_acc']
      net.load_state_dict(checkpoint['state_dict'])
      optimizer.load_state_dict(checkpoint['optimizer'])
      print('Model restored from epoch:', start_epoch)

  if args.evaluate:
    # Evaluate clean accuracy first because test_c mutates underlying data
    test_loss, test_acc = test(net, test_loader)
    print('Clean\n\tTest Loss {:.3f} | Test Error {:.2f}'.format(
        test_loss, 100 - 100. * test_acc))

    test_c_acc = test_c(net, test_data, base_c_path)
    print('Mean Corruption Error: {:.3f}'.format(100 - 100. * test_c_acc))
    return

  '''scheduler = torch.optim.lr_scheduler.LambdaLR(  #By default Scheduler # Schedular is used to update the learning rate
      optimizer,
      lr_lambda=lambda step: get_lr(  # pylint: disable=g-long-lambda
          step,
          args.epochs * len(train_loader),
          1,  # lr_lambda computes multiplicative factor
          1e-6 / args.learning_rate))'''
  
  if args.scheduler == 'lambda': 
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: get_lr(  # pylint: disable=g-long-lambda
            step,
            args.epochs * len(train_loader),
            1,  # lr_lambda computes multiplicative factor
            1e-6 / args.learning_rate))
  elif args.scheduler == 'cosineannealing':
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
      optimizer=optimizer,
      T_max=args.epochs * len(train_loader),
      eta_min= 1e-6 / args.learning_rate) 
  
  
  
  if not os.path.exists(args.save):
    os.makedirs(args.save)
  if not os.path.isdir(args.save):
    raise Exception('%s is not a dir' % args.save)

  log_path = os.path.join(args.save,
                          args.dataset + '_' + args.model + '_training_log.csv')
  with open(log_path, 'w') as f:
    f.write('epoch,time(s),train_loss,test_loss,test_error(%)\n')

  best_acc = 0

  writer = SummaryWriter(args.save)


  print('Beginning training from epoch:', start_epoch + 1)
  for epoch in range(start_epoch, args.epochs):
    begin_time = time.time()

    train_loss_ema = train(net, train_loader, optimizer, scheduler)
    test_loss, test_acc = test(net, test_loader)

    writer.add_scalar('Training Loss', train_loss_ema, epoch)
    writer.add_scalar('Test Loss', test_loss, epoch)
    writer.add_scalar('Test_Accuracy', test_acc, epoch)

    is_best = test_acc > best_acc
    best_acc = max(test_acc, best_acc)
    checkpoint = {
        'epoch': epoch,
        'dataset': args.dataset,
        'model': args.model,
        'state_dict': net.state_dict(),
        'best_acc': best_acc,
        'optimizer': optimizer.state_dict(),
    }

    save_path = os.path.join(args.save, 'checkpoint.pth.tar')
    torch.save(checkpoint, save_path)
    if is_best:
      shutil.copyfile(save_path, os.path.join(args.save, 'model_best.pth.tar'))

    with open(log_path, 'a') as f:
      f.write('%03d,%05d,%0.6f,%0.5f,%0.2f\n' % (
          (epoch + 1),
          time.time() - begin_time,
          train_loss_ema,
          test_loss,
          100 - 100. * test_acc,
      ))

    print(
        'Epoch {0:3d} | Time {1:5d} | Train Loss {2:.4f} | Test Loss {3:.3f} |'
        ' Test Error {4:.2f}'
        .format((epoch + 1), int(time.time() - begin_time), train_loss_ema,
                test_loss, 100 - 100. * test_acc))
  
  test_c_acc = test_c(net, test_data, base_c_path)
  print('Mean Corruption Error: {:.3f}'.format(100 - 100. * test_c_acc))
  writer.add_scalars('Mean Corruption Error',{'test_c_acc':100 - 100. * test_c_acc},global_step=1)
  
  if args.dataset == 'cifar10':
    test_fp = test_p(net, base_p_path,num_classes,writer)
    print('Mean Perturbation Flipping Rate: {:.3f}'.format(test_fp))
    writer.add_scalars('Mean Perturbation Flipping Rate',{'mean_p_flipping_rate':test_fp},global_step=1)
  
  writer.close()

  with open(log_path, 'a') as f:
    f.write('%03d,%05d,%0.6f,%0.5f,%0.2f\n' %
            (args.epochs + 1, 0, 0, 0, 100 - 100 * test_c_acc))


if __name__ == '__main__':
  main()
