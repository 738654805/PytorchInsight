from __future__ import print_function
import sys

import argparse
import os
import shutil
import time
import random
import numpy as np
import math

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim as optim
import torch.utils.data as data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
import models.imagenet as customized_models
from flops_counter import get_model_complexity_info
from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

from utils import Bar, Logger, AverageMeter, accuracy, mkdir_p

import warnings
warnings.filterwarnings('ignore')

try:
    from apex.parallel import DistributedDataParallel as DDP
    from apex.fp16_utils import *
    from apex import amp, optimizers
except ImportError:
    raise ImportError("Please install apex from https://www.github.com/nvidia/apex to run this example.")

# for servers to immediately record the logs
def flush_print(func):
    def new_print(*args, **kwargs):
        func(*args, **kwargs)
        sys.stdout.flush()
    return new_print
print = flush_print(print)


# Models
default_model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

customized_models_names = sorted(name for name in customized_models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(customized_models.__dict__[name]))

for name in customized_models.__dict__:
    if name.islower() and not name.startswith("__") and callable(customized_models.__dict__[name]):
        models.__dict__[name] = customized_models.__dict__[name]

model_names = default_model_names + customized_models_names

# Parse arguments
parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')

# Datasets
parser.add_argument('-d', '--data', default='path to dataset', type=str)
parser.add_argument('-j', '--workers', default=32, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
# Optimization options
parser.add_argument('--opt-level', default='O2', type=str, 
        help='O2 is mixed FP16/32 training, see more in https://github.com/NVIDIA/apex/tree/f5cd5ae937f168c763985f627bbf850648ea5f3f/examples/imagenet')
parser.add_argument('--keep-batchnorm-fp32', default=True, action='store_true',
                    help='keeping cudnn bn leads to fast training')
parser.add_argument('--loss-scale', type=float, default=None)

parser.add_argument('--mixup', dest='mixup', action='store_true',
                    help='whether to use mixup')
parser.add_argument('--alpha', default=0.2, type=float,
                    metavar='mixup alpha', help='alpha value for mixup B(alpha, alpha) distribution')
parser.add_argument('--cos', dest='cos', action='store_true', 
                    help='using cosine decay lr schedule')
parser.add_argument('--warmup', '--wp', default=5, type=int,
                    help='number of epochs to warmup')
parser.add_argument('--epochs', default=120, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--train-batch', default=256, type=int, metavar='N',
                    help='train batchsize (default: 256)')
parser.add_argument('--test-batch', default=125, type=int, metavar='N',
                    help='test batchsize (default: 200)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--drop', '--dropout', default=0, type=float,
                    metavar='Dropout', help='Dropout ratio')
parser.add_argument('--schedule', type=int, nargs='+', default=[30, 60, 90],
                        help='Decrease learning rate at these epochs.')
parser.add_argument('--gamma', type=float, default=0.1, help='LR is multiplied by gamma on schedule.')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
# Checkpoints
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('-c', '--checkpoint', default='checkpoint', type=str, metavar='PATH',
                    help='path to save checkpoint (default: checkpoint)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
# Architecture
parser.add_argument('--modelsize', '-ms', metavar='large', default='large', \
                    choices=['large', 'small'], \
                    help = 'model_size affects the data augmentation, please choose:' + \
                           ' large or small ')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('--depth', type=int, default=29, help='Model depth.')
parser.add_argument('--cardinality', type=int, default=32, help='ResNet cardinality (group).')
parser.add_argument('--base-width', type=int, default=4, help='ResNet base width.')
parser.add_argument('--widen-factor', type=int, default=4, help='Widen factor. 4 -> 64, 8 -> 128, ...')
# Miscs
parser.add_argument('--manualSeed', type=int, help='manual seed')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
#Device options
#parser.add_argument('--gpu-id', default='0', type=str, help='id(s) for CUDA_VISIBLE_DEVICES')
parser.add_argument('--local_rank', default=0, type=int)

args = parser.parse_args()
state = {k: v for k, v in args._get_kwargs()}

print("opt_level = {}".format(args.opt_level))
print("keep_batchnorm_fp32 = {}".format(args.keep_batchnorm_fp32), type(args.keep_batchnorm_fp32))
print("loss_scale = {}".format(args.loss_scale), type(args.loss_scale))

# Use CUDA
# os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id
use_cuda = torch.cuda.is_available()

# Random seed
if args.manualSeed is None:
    args.manualSeed = random.randint(1, 10000)
random.seed(args.manualSeed)
torch.manual_seed(args.manualSeed)
if use_cuda:
    torch.cuda.manual_seed_all(args.manualSeed)

best_acc = 0  # best test accuracy

def fast_collate(batch):
    imgs = [img[0] for img in batch]
    targets = torch.tensor([target[1] for target in batch], dtype=torch.int64)
    w = imgs[0].size[0]
    h = imgs[0].size[1]
    tensor = torch.zeros((len(imgs), 3, h, w), dtype=torch.uint8)
    for i, img in enumerate(imgs):
        nump_array = np.asarray(img, dtype=np.uint8)
        # tens = torch.from_numpy(nump_array)
        if nump_array.ndim < 3:
            nump_array = np.expand_dims(nump_array, axis=-1)
        nump_array = np.rollaxis(nump_array, 2)

        tensor[i] += torch.from_numpy(nump_array)

    return tensor, targets

class data_prefetcher():
    def __init__(self, loader):
        self.loader = iter(loader)
        self.stream = torch.cuda.Stream()
        self.mean   = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255]) \
                .cuda().view(1, 3, 1, 1)
        self.std    = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255]) \
                .cuda().view(1, 3, 1, 1)
        self.preload()

    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loader)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        
        with torch.cuda.stream(self.stream):
            self.next_input = self.next_input.cuda(non_blocking=True)
            self.next_target = self.next_target.cuda(non_blocking=True)
            self.next_input = self.next_input.float()
            self.next_input = self.next_input.sub_(self.mean).div_(self.std)

    def next(self):
        torch.cuda.current_stream().wait_stream(self.stream)
        input = self.next_input
        target = self.next_target
        if input is not None:
            self.preload()
        return input, target


def main():
    global best_acc
    start_epoch = args.start_epoch  # start from epoch 0 or last checkpoint epoch

    if not os.path.isdir(args.checkpoint) and args.local_rank == 0:
        mkdir_p(args.checkpoint)

    args.distributed = True
    args.gpu = args.local_rank
    torch.cuda.set_device(args.gpu)
    torch.distributed.init_process_group(backend='nccl', init_method='env://')
    args.world_size = torch.distributed.get_world_size()
    print('world_size = ', args.world_size)

    assert torch.backends.cudnn.enabled, "Amp requires cudnn backend to be enabled."

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    elif 'resnext' in args.arch:
        model = models.__dict__[args.arch](
                    baseWidth=args.base_width,
                    cardinality=args.cardinality,
                )
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    flops, params = get_model_complexity_info(model, (224, 224), as_strings=False, print_per_layer_stat=False)
    print('Flops:  %.3f' % (flops / 1e9))
    print('Params: %.2fM' % (params / 1e6))

    cudnn.benchmark = True
    # define loss function (criterion) and optimizer
    # criterion = nn.CrossEntropyLoss().cuda()
    criterion = SoftCrossEntropyLoss(label_smoothing=0.1).cuda()
    model = model.cuda()

    args.lr = float(0.1 * float(args.train_batch*args.world_size)/256.)

    optimizer = set_optimizer(model)
    #optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    model, optimizer = amp.initialize(model, optimizer,
                                      opt_level=args.opt_level,
                                      keep_batchnorm_fp32=args.keep_batchnorm_fp32,
                                      loss_scale=args.loss_scale)

    #model = torch.nn.DataParallel(model).cuda()
    #model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank], output_device=args.local_rank)
    model = DDP(model, delay_allreduce=True)

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'valf')
    #normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

    data_aug_scale = (0.08, 1.0) if args.modelsize == 'large' else (0.2, 1.0)

    train_dataset = datasets.ImageFolder(traindir, transforms.Compose([
            transforms.RandomResizedCrop(224, scale = data_aug_scale),
            transforms.RandomHorizontalFlip(),
            # transforms.ToTensor(),
            # normalize,
        ]))
    val_dataset   = datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            # transforms.ToTensor(),
            # normalize,
        ]))

    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.train_batch, shuffle=False,
        num_workers=args.workers, pin_memory=True, sampler=train_sampler, collate_fn=fast_collate)
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=args.test_batch, shuffle=False,
        num_workers=args.workers, pin_memory=True, sampler=val_sampler, collate_fn=fast_collate)


    # Resume
    title = 'ImageNet-' + args.arch
    if args.resume:
        # Load checkpoint.
        print('==> Resuming from checkpoint..', args.resume)
        assert os.path.isfile(args.resume), 'Error: no checkpoint directory found!'
        args.checkpoint = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_acc = checkpoint['best_acc']
        start_epoch = checkpoint['epoch']
        # model may have more keys
        t = model.state_dict()
        c = checkpoint['state_dict']
        flag = True 
        for k in t:
            if k not in c:
                print('not in loading dict! fill it', k, t[k])
                c[k] = t[k]
                flag = False
        model.load_state_dict(c)
        if flag:
            print('optimizer load old state')
            optimizer.load_state_dict(checkpoint['optimizer'])
        else:
            print('new optimizer !')
        if args.local_rank == 0:
            logger = Logger(os.path.join(args.checkpoint, 'log.txt'), title=title, resume=True)
    else:
        if args.local_rank == 0:
            logger = Logger(os.path.join(args.checkpoint, 'log.txt'), title=title)
            logger.set_names(['Learning Rate', 'Train Loss', 'Valid Loss', 'Train Acc.', 'Valid Acc.'])


    if args.evaluate:
        print('\nEvaluation only')
        test_loss, test_acc = test(val_loader, model, criterion, start_epoch, use_cuda)
        print(' Test Loss:  %.8f, Test Acc:  %.2f' % (test_loss, test_acc))
        return

    scheduler = CosineAnnealingLR(optimizer,
        args.epochs, len(train_loader), eta_min=0., warmup=args.warmup) 

    # Train and val
    for epoch in range(start_epoch, args.epochs):
        train_sampler.set_epoch(epoch)

        #adjust_learning_rate(optimizer, epoch)

        if args.local_rank == 0:
            print('\nEpoch: [%d | %d]' % (epoch + 1, args.epochs))

        train_loss, train_acc = train(train_loader, model, criterion, optimizer, epoch, scheduler, use_cuda)
        test_loss, test_acc = test(val_loader, model, criterion, epoch, use_cuda)

        # save model
        if args.local_rank == 0:
            # append logger file
            logger.append([state['lr'], train_loss, test_loss, train_acc, test_acc])

            is_best = test_acc > best_acc
            best_acc = max(test_acc, best_acc)
            save_checkpoint({
                    'epoch': epoch + 1,
                    'state_dict': model.state_dict(),
                    'acc': test_acc,
                    'best_acc': best_acc,
                    'optimizer' : optimizer.state_dict(),
                }, is_best, checkpoint=args.checkpoint)

    if args.local_rank == 0:
        logger.close()

    print('Best acc:')
    print(best_acc)

def train(train_loader, model, criterion, optimizer, epoch, scheduler, use_cuda):
    # switch to train mode
    model.train()
    torch.set_grad_enabled(True)

    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    if args.local_rank == 0:
        bar = Bar('Processing', max=len(train_loader))
    show_step = len(train_loader) // 10
    
    prefetcher = data_prefetcher(train_loader)
    inputs, targets = prefetcher.next()

    batch_idx = -1
    while inputs is not None:
    # for batch_idx, (inputs, targets) in enumerate(train_loader):
        batch_idx += 1
        lr = scheduler.update(epoch, batch_idx)

        batch_size = inputs.size(0)
        if batch_size < args.train_batch:
            break
        # measure data loading time

        #if use_cuda:
        #    inputs, targets = inputs.cuda(), targets.cuda(async=True)
        #inputs, targets = torch.autograd.Variable(inputs), torch.autograd.Variable(targets)

        if args.mixup:
            inputs, targets_a, targets_b, lam = mixup_data(inputs, targets, args.alpha, use_cuda)
            outputs = model(inputs)
            loss_func = mixup_criterion(targets_a, targets_b, lam)
            old_loss = loss_func(criterion, outputs)
        else:
            outputs = model(inputs)
            old_loss = criterion(outputs, targets)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        # loss.backward()
        with amp.scale_loss(old_loss, optimizer) as loss:
            loss.backward()
        optimizer.step()


        if batch_idx % args.print_freq == 0:
            # measure accuracy and record loss
            prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
            reduced_loss = reduce_tensor(loss.data)
            prec1        = reduce_tensor(prec1)
            prec5        = reduce_tensor(prec5)

            # to_python_float incurs a host<->device sync
            losses.update(to_python_float(reduced_loss), inputs.size(0))
            top1.update(to_python_float(prec1), inputs.size(0))
            top5.update(to_python_float(prec5), inputs.size(0))

            torch.cuda.synchronize()
            # measure elapsed time
            batch_time.update((time.time() - end) / args.print_freq)
            end = time.time()

            if args.local_rank == 0: # plot progress
                bar.suffix  = '({batch}/{size}) lr({lr:.6f}) | Batch: {bt:.3f}s | Total: {total:} | Loss: {loss:.4f} | top1: {top1: .4f} | top5: {top5: .4f}'.format(
                            lr=lr[0],
                            batch=batch_idx + 1,
                            size=len(train_loader),
                            bt=batch_time.val,
                            total=bar.elapsed_td,
                            loss=losses.avg,
                            top1=top1.avg,
                            top5=top5.avg,
                            )
                bar.next()
        if (batch_idx) % show_step == 0 and args.local_rank == 0:
            print('E%d' % (epoch) + bar.suffix)

        inputs, targets = prefetcher.next()

    if args.local_rank == 0:
        bar.finish()
    return (losses.avg, top1.avg)

def test(val_loader, model, criterion, epoch, use_cuda):
    global best_acc

    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()
    # torch.set_grad_enabled(False)

    end = time.time()
    if args.local_rank == 0:
        bar = Bar('Processing', max=len(val_loader))

    prefetcher = data_prefetcher(val_loader)
    inputs, targets = prefetcher.next()

    batch_idx = -1
    while inputs is not None:
    # for batch_idx, (inputs, targets) in enumerate(val_loader):
        batch_idx += 1

        #if use_cuda:
        #    inputs, targets = inputs.cuda(), targets.cuda()
        #inputs, targets = torch.autograd.Variable(inputs, volatile=True), torch.autograd.Variable(targets)

        # compute output
        with torch.no_grad():
            outputs = model(inputs)
            loss = criterion(outputs, targets)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))

        reduced_loss = reduce_tensor(loss.data)
        prec1        = reduce_tensor(prec1)
        prec5        = reduce_tensor(prec5)

        # to_python_float incurs a host<->device sync
        losses.update(to_python_float(reduced_loss), inputs.size(0))
        top1.update(to_python_float(prec1), inputs.size(0))
        top5.update(to_python_float(prec5), inputs.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        if args.local_rank == 0:
            bar.suffix  = 'Valid({batch}/{size}) | Batch: {bt:.3f}s | Total: {total:} | Loss: {loss:.4f} | top1: {top1: .4f} | top5: {top5: .4f}'.format(
                        batch=batch_idx + 1,
                        size=len(val_loader),
                        bt=batch_time.avg,
                        total=bar.elapsed_td,
                        loss=losses.avg,
                        top1=top1.avg,
                        top5=top5.avg,
                        )
            bar.next()

        inputs, targets = prefetcher.next()

    if args.local_rank == 0:
        print(bar.suffix)
        bar.finish()
    return (losses.avg, top1.avg)

def save_checkpoint(state, is_best, checkpoint='checkpoint', filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint, 'model_best.pth.tar'))

def set_optimizer(model):
    params = [{'params': [p for name, p in model.named_parameters() if \
            ('bias' in name or 'bn' in name)], 'weight_decay': 0} \
            , {'params': [p for name, p in model.named_parameters() if \
            ('bias' not in name and 'bn' not in name)]}]
    names = [{'params': [name for name, p in model.named_parameters() if \
            ('bias' in name or 'bn' in name)], 'weight_decay': 0} \
            , {'params': [name for name, p in model.named_parameters() if \
            ('bias' not in name and 'bn' not in name)]}]
    print('optimizer group names:', names)
    optimizer = optim.SGD(params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    print('optimizer = ', optimizer)
    return optimizer

def adjust_learning_rate(optimizer, epoch):
    global state

    def adjust_optimizer():
        for param_group in optimizer.param_groups:
            param_group['lr'] = state['lr']

    if epoch < args.warmup:
        state['lr'] = args.lr * (epoch + 1) / args.warmup
        adjust_optimizer()

    elif args.cos: # cosine decay lr schedule (Note: epoch-wise, not batch-wise)
        state['lr'] = args.lr * 0.5 * (1 + np.cos(np.pi * epoch / args.epochs))
        adjust_optimizer()

    elif epoch in args.schedule: # step lr schedule
        state['lr'] *= args.gamma
        adjust_optimizer()

class SoftCrossEntropyLoss(nn.NLLLoss):
    def __init__(self, label_smoothing=0, num_classes=1000, **kwargs):
        assert label_smoothing >= 0 and label_smoothing <= 1
        super(SoftCrossEntropyLoss, self).__init__(**kwargs)
        self.confidence = 1 - label_smoothing
        self.other      = label_smoothing * 1.0 / (num_classes - 1)
        self.criterion  = nn.KLDivLoss(reduction='batchmean')
        print('using soft celoss!!!, label_smoothing = ', label_smoothing)

    def forward(self, input, target):
        one_hot = torch.zeros_like(input)
        one_hot.fill_(self.other)
        one_hot.scatter_(1, target.unsqueeze(1).long(), self.confidence)
        input   = F.log_softmax(input, 1)
        return self.criterion(input, one_hot)

def mixup_data(x, y, alpha=1.0, use_cuda=True):
    if alpha > 0.:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1.

    batch_size = x.size(0)
    if use_cuda:
        index = torch.randperm(batch_size).cuda()
    else:
        index = torch.randperm(batch_size)

    mixed_x = lam * x + (1 - lam) * x[index, ...]
    y_a, y_b = y, y[index]
    return mixed_x, y_a, y_b, lam

def mixup_criterion(y_a, y_b, lam):
    return lambda criterion, pred: lam * criterion(pred, y_a) + (1 - lam) * criterion(pred, y_b)

def reduce_tensor(tensor):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= args.world_size
    return rt

class CosineAnnealingLR(object):
    def __init__(self, optimizer, T_max, N_batch, eta_min=0, last_epoch=-1, warmup=0):
        if not isinstance(optimizer, torch.optim.Optimizer):
            raise TypeError('{} is not an Optimizer'.format(
                type(optimizer).__name__))
        self.optimizer = optimizer
        self.T_max = T_max
        self.N_batch = N_batch
        self.eta_min = eta_min
        self.warmup = warmup

        if last_epoch == -1:
            for group in optimizer.param_groups:
                group.setdefault('initial_lr', group['lr'])
        else:
            for i, group in enumerate(optimizer.param_groups):
                if 'initial_lr' not in group:
                    raise KeyError("param 'initial_lr' is not specified "
                                   "in param_groups[{}] when resuming an optimizer".format(i))
        self.base_lrs = list(map(lambda group: group['initial_lr'], optimizer.param_groups))
        self.update(last_epoch+1)
        self.last_epoch = last_epoch
        self.iter = 0

    def state_dict(self):
        return {key: value for key, value in self.__dict__.items() if key != 'optimizer'}

    def load_state_dict(self, state_dict):
        self.__dict__.update(state_dict)

    def get_lr(self):
        if self.last_epoch < self.warmup:
            lrs = [base_lr * (self.last_epoch + self.iter / self.N_batch) / self.warmup for base_lr in self.base_lrs]
        else:
            lrs = [self.eta_min + (base_lr - self.eta_min) *
                    (1 + math.cos(math.pi * (self.last_epoch - self.warmup + self.iter / self.N_batch) / (self.T_max - self.warmup))) / 2
                    for base_lr in self.base_lrs]
        return lrs

    def update(self, epoch, batch=0):
        self.last_epoch = epoch
        self.iter = batch + 1
        lrs = self.get_lr()
        for param_group, lr in zip(self.optimizer.param_groups, lrs):
            param_group['lr'] = lr

        return lrs
if __name__ == '__main__':
    main()
