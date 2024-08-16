import os
import argparse
import logging
import math
import random
import numpy as np
from itertools import chain
from time import time
from datetime import datetime
from tqdm.auto import tqdm
import wandb
from PIL import Image

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision.utils import make_grid
import torch.nn.functional as F

from transformers import get_scheduler
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed

from models import VQVAE, VAR, build_var, ControlVAR, build_control_var
from utils.wandb import CustomWandbTracker
from ruamel.yaml import YAML

from peft import LoraConfig, get_peft_model, TaskType
from cp_dataset import CPDatasetV2

logger = get_logger(__name__)

device = torch.device('cuda')


def parse_args():
    parser = argparse.ArgumentParser()

    # config file
    parser.add_argument("--config",
                        type=str,
                        default='configs/train_mask_var_ImageNetS_local.yaml',
                        help="config file used to specify parameters")

    # data
    parser.add_argument("--data", type=str, default=None, help="data")
    # parser.add_argument("--data_dir", type=str, default='/voyager/ImageNet2012', help="data folder")
    parser.add_argument("--data_dir", type=str, default='/opt/disk1/dwang/sci/DVTON/data/VITON-HD/', help="data folder")
    parser.add_argument("--data_list", type=str, default='train_pairs.txt')
    parser.add_argument("--val_data_list", type=str, default='subtrain_1.txt')

    parser.add_argument("--dataset_name", type=str, default="imagenetM", help="dataset name")
    parser.add_argument("--image_size", type=int, default=256, help="image size")
    parser.add_argument("--batch_size", type=int, default=8, help="per gpu batch size")
    parser.add_argument("--num_workers", type=int, default=16, help="batch size")

    # training
    parser.add_argument("--debug", type=bool, default=False)
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--run_name", type=str, default=None, help="run_name")
    parser.add_argument("--output_dir", type=str, default="experiments", help="output folder")
    parser.add_argument("--num_epochs", type=int, default=10000)
    parser.add_argument("--optimizer", type=str, default="adamw", help="optimizer")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="learning rate")
    parser.add_argument("--lr_scheduler", type=str, default='linear', help='lr scheduler')
    parser.add_argument("--log_interval", type=int, default=500, help='log interval for steps')
    parser.add_argument("--val_interval", type=int, default=5, help='validation interval for epochs')
    parser.add_argument("--save_interval", type=str, default='1000', help='save interval')
    parser.add_argument("--mixed_precision",
                        type=str,
                        default='bf16',
                        help='mixed precision',
                        choices=['no', 'fp16', 'bf16', 'fp8'])
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1, help='gradient accumulation steps')
    parser.add_argument("--lora", type=bool, default=False, help='use lora to train linear layers only')
    parser.add_argument("--clip", type=float, default=2., help='gradient clip, set to -1 if not used')
    parser.add_argument("--wp0", type=float, default=0.005, help='initial lr ratio at the begging of lr warm up')
    parser.add_argument("--wpe", type=float, default=0.01, help='final lr ratio at the end of training')
    parser.add_argument("--weight_decay", type=float, default=0.05, help="weight decay")
    parser.add_argument("--weight_decay_end", type=float, default=0, help='final lr ratio at the end of training')
    parser.add_argument("--resume", type=bool, default=False, help='resume')
    # vqvae
    parser.add_argument("--vocab_size", type=int, default=4096, nargs='+', help="codebook size")
    parser.add_argument("--z_channels", type=int, default=32, help="latent size of vqvae")
    parser.add_argument("--ch", type=int, default=160, help="channel size of vqvae")
    parser.add_argument("--vqvae_pretrained_path",
                        type=str,
                        default='pretrained/vae_ch160v4096z32.pth',
                        help="vqvae pretrained path")
    # parser.add_argument("--var_pretrained_path", type=str, default='pretrained/var_d16.pth', help="var pretrained path")
    parser.add_argument("--var_pretrained_path", default=None, help="var pretrained path")
    # vpq model
    parser.add_argument("--v_patch_nums",
                        type=int,
                        default=[1, 2, 3, 4, 5, 6, 8, 10, 13, 16],
                        help="number of patch numbers of each scale")
    parser.add_argument("--v_patch_layers",
                        type=int,
                        default=[1, 2, 3, 4, 5, 6, 8, 10, 13, 16],
                        help="index of layers for predicting each scale")
    parser.add_argument("--depth", type=int, default=16, help="depth of vpq model")
    parser.add_argument("--embed_dim", type=int, default=1024, help="embedding dimension of vpq model")
    parser.add_argument("--num_heads", type=int, default=16, help="number of heads of vpq model")
    parser.add_argument("--mlp_ratio", type=float, default=4.0, help="mlp ratio of vpq model")
    parser.add_argument("--drop_rate", type=float, default=0.0, help="drop rate of vpq model")
    parser.add_argument("--attn_drop_rate", type=float, default=0.0, help="attn drop rate of vpq model")
    parser.add_argument("--drop_path_rate", type=float, default=0.0, help="drop path rate of vpq model")
    parser.add_argument("--mask_type", type=str, default='interleave_append', help="[interleave_append, replace]")
    parser.add_argument("--uncond", type=bool, default=False, help="uncond gen")
    parser.add_argument("--bidirectional", type=bool, default=False, help="shuffle mask and image order in each stage")
    parser.add_argument("--separate_decoding",
                        type=bool,
                        default=False,
                        help="separate decode mask and image in each stage")
    parser.add_argument("--separator", type=bool, default=False, help="use special tokens as separator")
    parser.add_argument("--type_pos", type=bool, default=False, help="use type pos embed")
    parser.add_argument("--interpos", type=bool, default=False, help="interpolate positional encoding")
    parser.add_argument("--mpos", type=bool, default=False, help="minus positional encoding")
    parser.add_argument("--indep", type=bool, default=False, help="indep separate decoding")
    parser.add_argument("--multi_cond", type=bool, default=True, help="multi-type conditions")
    # condition model
    parser.add_argument("--condition_model", type=str, default="class_embedder", help="condition model")
    parser.add_argument("--num_classes", type=int, default=1000, help="number of classes for condition model")
    parser.add_argument("--cond_drop_rate", type=float, default=0.1, help="drop rate of condition model")

    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--c_mask", type=bool, default=None, help='teaching force mask in validation')
    parser.add_argument("--c_img", type=bool, default=True, help='teaching force img in validation')
    parser.add_argument("--cfg", nargs='+', type=float, default=[4, 4, 4], help='cfg guidance scale')
    parser.add_argument("--gibbs", type=int, default=0, help='use gibbs sampling during inference')
    parser.add_argument("--save_val", type=bool, default=False, help='save val images')


    # fFirst parse of command-line args to check for config file
    args = parser.parse_args()

    # If a config file is specified, load it and set defaults
    if args.config is not None:
        with open(args.config, 'r', encoding='utf-8') as f:
            yaml = YAML(typ='safe')
            with open(args.config, 'r', encoding='utf-8') as file:
                config_args = yaml.load(file)
            parser.set_defaults(**config_args)

    # re-parse command-line args to overwrite with any command-line inputs
    args = parser.parse_args()

    return args


def train_epoch(accelerator, var, vqvae, cond_model, dataloader, optimizer, lr_scheduler, progress_bar, args):

    var.train()
    if cond_model is not None:
        cond_model.train()
    # loss_fn = torch.nn.CrossEntropyLoss(reduction='none')# Dingkang
    loss_fn = torch.nn.CrossEntropyLoss()
    for batch_idx, batch in enumerate(dataloader):

        with accelerator.accumulate(var):
            images, inpaintB3WH, conditions, cond_type, clothB3WH = \
                batch['GT'], batch['inpaint_image'], batch['label'], batch['type'], batch["cloth_pure"]

            # cond_type = 0
            # label = 834

            # forward to get input ids
            with torch.no_grad():
                mask_inpaint_labels_list = vqvae.img_to_idxBl(inpaintB3WH, v_patch_nums=args.v_patch_nums)
                # from labels get inputs fhat list: List[(B, 2**2, 32), (B, 3**2, 32))]
                maskinpaint_input_h_list = vqvae.idxBl_to_h(mask_inpaint_labels_list)  # len 9

                                # mask_labels_list = vqvae.img_to_idxBl(resized_mask, v_patch_nums=args.v_patch_nums)
                mask_inpaint_labels_list = vqvae.img_to_idxBl(inpaintB3WH, v_patch_nums=args.v_patch_nums)
                # from labels get inputs fhat list: List[(B, 2**2, 32), (B, 3**2, 32))]
                maskinpaint_input_h_list = vqvae.idxBl_to_h(mask_inpaint_labels_list)  # len 9



                # labels_list: List[(B, 1), (B, 4), (B, 9)]
                labels_list = vqvae.img_to_idxBl(images, v_patch_nums=args.v_patch_nums)
                # from labels get inputs fhat list: List[(B, 2**2, 32), (B, 3**2, 32))]
                input_h_list = vqvae.idxBl_to_h(labels_list)

                # handle mask
                if args.mask_type == 'replace':
                    # Image: r1, r2, r3, Mask: m1, m2, m3
                    # New: r1, m2, r3
                    # Note that image goes first
                    for i in range(len(input_h_list)):
                        if i % 2 == 0:
                            labels_list[i] = mask_labels_list[i]
                            input_h_list[i] = mask_input_h_list[i]
                    mask_first = False
                elif args.mask_type == 'interleave_append':
                    # Image: r1, r2, r3, Mask: m1, m2, m3
                    # New: (m1, r1), (m2, r2), (m3, r3)
                    # Note that mask goes first unless bidirectional enabled
                    if args.bidirectional and random.random() < 0.5:
                        labels_list_ = list(chain.from_iterable(zip(labels_list, mask_labels_list)))
                        input_h_list_ = list(chain.from_iterable(zip(input_h_list, mask_input_h_list)))
                        mask_first = False
                    else:
                        labels_list_ = list(chain.from_iterable(zip(mask_labels_list, labels_list)))
                        # 20
                        input_h_list_ = list(chain.from_iterable(zip(mask_input_h_list, input_h_list)))
                        # 18
                        mask_first = True
                    labels_list, input_h_list = labels_list_, input_h_list_
                else:
                    raise NotImplementedError
            x_BLCv_wo_first_l = torch.concat(input_h_list, dim=1)

            # forwad through model
            logits = var(conditions, x_BLCv_wo_first_l, mask_first=mask_first, cond_type=cond_type)  # BLC, C=vocab size
            logits = logits.view(-1, logits.size(-1))
            labels = torch.cat(labels_list, dim=1)
            labels = labels.view(-1)

            loss = loss_fn(logits, labels)

            ignore_mask = batch['ignore_mask'] if mask_first else batch['ignore_mask_']
            ignore_mask = ignore_mask.view(-1)
            loss = (loss * ignore_mask.float()).mean() / (ignore_mask.mean() + 1e-6)
            print("loss", float(loss))
            accelerator.backward(loss)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

        # Checks if the accelerator has performed an optimization step behind the scenes
        if accelerator.sync_gradients:
            progress_bar.update(1)
            args.completed_steps += 1

        # Log metrics
        if args.completed_steps % args.log_interval == 0:
            accelerator.log(
                {
                    "train/loss": loss.item(),
                    "step": args.completed_steps,
                    "epoch": args.epoch,
                    "lr": optimizer.param_groups[0]["lr"]
                },
                step=args.completed_steps)

        # Save model
        # if isinstance(args.save_interval, int):
        #     if epoch == 0:
        #         save_dir = os.path.join(args.project_dir, f"step_{args.completed_steps}")
        #         os.makedirs(save_dir, exist_ok=True)
        #         accelerator.save_state(save_dir)

        # TODO remove
        # if args.completed_steps % 100 == 0:
        #     inference(accelerator,
        #               var,
        #               vqvae,
        #               cond_model,
        #               np.random.choice(args.num_classes, 4).tolist(),
        #               num_samples=1,
        #               guidance_scale=4.0,
        #               top_k=900,
        #               top_p=0.95,
        #               seed=42)


def cls_cond_inference(cls, device, B, var, index, cond_type, guidance_scale, top_k, top_p, seed):
    types = {'mask': 0, 'canny': 1, 'depth': 2, 'normal': 3, 'none': 4}
    conditions = torch.tensor([cls for _ in range(B)], device=device).long()
    cond_type = torch.tensor([types[cond_type] for _ in range(B)], device=device).long()
    with torch.no_grad():
        images = var.autoregressive_infer_cfg(B=B,
                                              label_B=conditions,
                                              cond_type=cond_type,
                                              cfg=guidance_scale[0],
                                              top_k=top_k,
                                              top_p=top_p,
                                              g_seed=seed)
    return images


@torch.no_grad()
def inference(accelerator,
              var,
              vqvae,
              cond_model,
              conditions,
              num_samples=1,
              guidance_scale=4.0,
              top_k=900,
              top_p=0.95,
              seed=42):

    var.eval()
    cond_model.eval()
    images = var.autoregressive_infer_cfg(B=len(conditions),
                                          label_B=torch.tensor(conditions, device=torch.device('cuda')),
                                          cfg=4,
                                          top_k=top_k,
                                          top_p=top_p,
                                          g_seed=seed)
    image = make_grid(images, nrow=len(conditions), padding=0, pad_value=1.0)
    image = image.permute(1, 2, 0).mul_(255).cpu().numpy()
    image = Image.fromarray(image.astype(np.uint8))

    accelerator.log({"inference/images": [wandb.Image(image, caption=f"{conditions}")]})
    var.train()
    cond_model.train()


def pix_cond_inference(images, masks, conditions, cond_type, device, B, var, vqvae, c_mask, c_img, guidance_scale,
                       top_k, top_p, seed, args):
    types = {'mask': 0, 'canny': 1, 'depth': 2, 'normal': 3, 'none': 4}
    images = images.to(device)
    masks = masks.to(device)
    if isinstance(conditions, int):
        conditions = torch.tensor([conditions for _ in range(B)]).to(device)
    else:
        conditions = conditions.to(device)  # cls
    if isinstance(cond_type, str):
        cond_type = torch.tensor([types[cond_type] for _ in range(B)], device=device)
    else:
        cond_type = cond_type.to(device)

    with torch.no_grad():
        if c_mask:
            c_mask = vqvae.img_to_idxBl(masks, v_patch_nums=args.v_patch_nums)
        elif c_img:
            c_img = vqvae.img_to_idxBl(images, v_patch_nums=args.v_patch_nums)
        else:
            c_mask, c_img = None, None

        images = var.conditional_infer_cfg(B=B,
                                           label_B=conditions,
                                           cfg=guidance_scale,
                                           top_k=top_k,
                                           top_p=top_p,
                                           g_seed=seed,
                                           c_mask=c_mask,
                                           c_img=c_img,
                                           cond_type=cond_type)
        # htcore.mark_step()
    return images


def validate(var,
             vqvae,
             cond_model,
             dataloader,
             args,
             guidance_scale=(6, 6, 6),
             top_k=900,
             top_p=0.96,
             seed=42,
             c_mask=None,
             c_img=None,
             rank=0,
             gibbs=0,
             save_val=True,
             device=None):
    var.eval()

    if cond_model:
        cond_model.eval()
    if c_mask or c_img:
        pbar = tqdm(range(math.ceil(len(dataloader))), disable=not rank == 0)
        save_path = os.path.join(args.project_dir, f'cfg_{guidance_scale[0]}_{guidance_scale[1]}_{guidance_scale[2]}',
                                 f'{rank}')
        os.makedirs(save_path, exist_ok=True)
        for batch_idx, batch in enumerate(dataloader):
            # images, masks, conditions, cond_type = batch['image'], batch['mask'], batch['cls'], batch['type']
            images, inpaintB3WH, conditions, cond_type, clothB3WH, masks = \
                batch['GT'], batch['inpaint_image'], batch['label'], batch['type'], batch["cloth_pure"], batch["mask"]

            B = masks.shape[0]
            images = pix_cond_inference(images, masks, conditions, cond_type, device, B, var, vqvae, c_mask, c_img,
                                        guidance_scale, top_k, top_p, seed, args)
            if save_val:
                images = images.permute(0, 2, 3, 1).mul_(255).cpu().numpy().astype(np.uint8)
                for b in range(B):
                    image = Image.fromarray(images[b, 256:])
                    image.save(os.path.join(save_path, f'{batch_idx * B + b}.png'))
            else:
                image_ = make_grid(images, nrow=B, padding=0, pad_value=1.0)
                image_ = image_.permute(1, 2, 0).mul_(255).cpu().numpy()
                image_ = Image.fromarray(image_.astype(np.uint8))
                wandb.log({f"val/images": [wandb.Image(image_, caption=f"{conditions}_{guidance_scale}")]})

            pbar.update(1)
    else:
        slices = 1000 // args.gpus
        classes = [i for i in range(slices * rank, slices * (rank + 1))] if rank != args.gpus - 1 \
            else [i for i in range(slices * rank, 1000)]
        pbar = tqdm(range(len(classes)), disable=not rank == 0)
        for cls in classes:
            os.makedirs(os.path.join(args.project_dir, f'cfg_{guidance_scale[0]}', f'{cls}'), exist_ok=True)
            assert 50 > args.batch_size
            for i in range(50 // args.batch_size + 1):
                B = args.batch_size if i != 50 // args.batch_size else 50 - i * args.batch_size
                if B == 0:
                    continue
                cond_type = 'depth'
                seed = seed + i * (cls + 1)
                images = cls_cond_inference(cls, device, B, var, i, cond_type, guidance_scale, top_k, top_p, seed)
                # image = make_grid(images, nrow=B, padding=0, pad_value=1.0)
                if gibbs != 0:
                    for g_step in range(gibbs):
                        # start from mask teaching force
                        masks, images = images[:, :, :256, :], images[:, :, 256:, :]
                        masks, images = (masks - 0.5) / 0.5, (images - 0.5) / 0.5
                        c_mask = True
                        images = pix_cond_inference(images, masks, cls, cond_type, device, B, var, vqvae, c_mask, c_img,
                                                    guidance_scale, top_k, top_p, seed, args)

                        masks, images = images[:, :, :256, :], images[:, :, 256:, :]
                        masks, images = (masks - 0.5) / 0.5, (images - 0.5) / 0.5
                        c_img = True
                        images = pix_cond_inference(images, masks, cls, cond_type, device, B, var, vqvae, c_mask, c_img,
                                                    guidance_scale, top_k, top_p, seed, args)

                if save_val:
                    images = images.permute(0, 2, 3, 1).mul_(255).cpu().numpy().astype(np.uint8)
                    for b in range(B):
                        image = Image.fromarray(images[b, 256])
                        image.save(
                            os.path.join(args.project_dir, f'cfg_{guidance_scale[0]}', f'{cls}',
                                         f'{i * args.batch_size + b}.png'))
                else:
                    image_ = make_grid(images, nrow=B, padding=0, pad_value=1.0)
                    image_ = image_.permute(1, 2, 0).mul_(255).cpu().numpy()
                    image_ = Image.fromarray(image_.astype(np.uint8))
                    wandb.log({f"images": [wandb.Image(image_, caption=f"{cls}_{guidance_scale}")]})
            pbar.update(1)

    var.train()


def main():

    args = parse_args()

    # seed
    set_seed(args.seed)

    # Setup accelerator:
    if args.run_name is None:
        model_name = f'vqvae_ch{args.ch}v{args.vocab_size}z{args.z_channels}_vpa_d{args.depth}e{args.embed_dim}h{args.num_heads}_{args.dataset_name}_ep{args.num_epochs}_bs{args.batch_size}'
    else:
        model_name = args.run_name
    args.model_name = model_name
    timestamp = datetime.fromtimestamp(time()).strftime('%Y-%m-%d-%H-%M-%S')
    args.project_dir = f"{args.output_dir}/{timestamp}-{model_name}"  # Create an experiment folder
    os.makedirs(args.project_dir, exist_ok=True)
    save_interval = args.save_interval

    args.save_interval = save_interval

    accelerator = Accelerator(mixed_precision=args.mixed_precision,
                              gradient_accumulation_steps=args.gradient_accumulation_steps,
                              log_with=CustomWandbTracker(model_name),
                              project_dir=args.project_dir)
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)

    # create dataset
    logger.info("Creating dataset")
    # dataset = create_dataset(args.dataset_name, args)
    dataset = CPDatasetV2(args.data_dir,
                          256,
                          mode="train",
                          data_list=args.data_list,
                          v_patch_nums=args.v_patch_nums,
                          separator=args.separator)
    val_dataset = CPDatasetV2(args.data_dir,
                              256,
                              mode="train",
                              data_list=args.val_data_list,
                              v_patch_nums=args.v_patch_nums,
                              separator=args.separator)

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=1,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    # create dataloader
    # dataloader = DataLoader(dataset,
    #                         batch_size=args.batch_size,
    #                         shuffle=True,
    #                         num_workers=args.num_workers,
    #                         pin_memory=True,
    #                         drop_last=True)
    # Calculate total batch size
    total_batch_size = args.batch_size * accelerator.num_processes * args.gradient_accumulation_steps
    args.total_batch_size = total_batch_size

    # Create VQVAE Model
    logger.info("Creating VQVAE model")
    vqvae = VQVAE(vocab_size=args.vocab_size,
                  z_channels=args.z_channels,
                  ch=args.ch,
                  test_mode=True,
                  share_quant_resi=4,
                  v_patch_nums=args.v_patch_nums)
    vqvae.eval()
    for p in vqvae.parameters():
        p.requires_grad_(False)
    if args.vqvae_pretrained_path is not None:
        vqvae.load_state_dict(torch.load(args.vqvae_pretrained_path, map_location=torch.device('cpu')))

    # Create VPA Model
    logger.info("Creating VAR model")

    print("Creating VAR model")

    var = build_control_var(vae=vqvae,
                            depth=args.depth,
                            patch_nums=args.v_patch_nums,
                            mask_type=args.mask_type,
                            cond_drop_rate=1.1 if args.uncond else 0.1,
                            bidirectional=args.bidirectional,
                            separate_decoding=args.separate_decoding,
                            separator=args.separator,
                            type_pos=args.type_pos,
                            indep=args.indep,
                            multi_cond=args.multi_cond)

    var, vqvae, val_dataloader, dataloader = accelerator.prepare(var, vqvae, val_dataloader, dataloader)

    if args.var_pretrained_path is not None:
        var_state_dict = torch.load(args.var_pretrained_path, map_location=torch.device('cpu'))
        init_std = math.sqrt(1 / args.embed_dim / 3)
        if args.mask_type == 'interleave_append':
            for key in ['lvl_1L', 'pos_start', 'attn_bias_for_masking']:
                del var_state_dict[key]  # will be handled in the init
            for key in [
                    'pos_1LC',
            ]:
                pos_1LC_ = var_state_dict[key]
                if args.separator:
                    pos_1LC = []
                    L = 0
                    for i, pn in enumerate(args.v_patch_nums):
                        num_sp_tokens = 1 if i != 0 else 0
                        pe = torch.empty((pn * pn + num_sp_tokens) * 2, args.embed_dim)
                        nn.init.trunc_normal_(pe, mean=0, std=init_std)
                        pe[:pn * pn] = pos_1LC_[:, L:L + pn * pn]
                        pe[pn * pn + num_sp_tokens:pn * pn * 2 + num_sp_tokens] = pos_1LC_[:, L:L + pn * pn]
                        pos_1LC.append(pe)
                        L += pn * pn
                    pos_1LC = torch.cat(pos_1LC, dim=0).unsqueeze(0)  # 1, L, C
                    var_state_dict[key] = pos_1LC
                else:
                    var_state_dict[key] = torch.concat([var_state_dict[key], var_state_dict[key]], dim=1)
            # key = 'lvl_embed.weight'
            # var_state_dict[key] = torch.concat([var_state_dict[key], var_state_dict[key]], dim=0)
            if args.separator:
                weight = torch.empty(args.vocab_size + (len(args.v_patch_nums) - 1) * 2, args.embed_dim)
                bias = torch.empty(args.vocab_size + (len(args.v_patch_nums) - 1) * 2)
                nn.init.trunc_normal_(weight, mean=0, std=init_std)
                nn.init.trunc_normal_(bias, mean=0, std=init_std)
                weight[:args.vocab_size] = var_state_dict['head.weight']
                bias[:args.vocab_size] = var_state_dict['head.bias']
                var_state_dict['head.weight'] = weight
                var_state_dict['head.bias'] = bias
        # var.load_state_dict(var_state_dict, strict=False)

    if args.lora:
        lora_params = []
        for name, _ in var.named_modules():
            if ('attn.' in name and 'attn.proj_drop' not in name) or 'ffn.fc' in name or 'ada_lin.1' in name:
                lora_params.append(name)
        # Define LoRA Config
        lora_config = LoraConfig(
            r=16,
            lora_alpha=32,
            target_modules=lora_params,
            lora_dropout=0.05,
            bias="none",
        )
        # add LoRA adaptor
        # var = prepare_model_for_kbit_training(var)
        var = get_peft_model(var, lora_config)
        var.print_trainable_parameters()

    var.train()

    # Create Condition Model
    logger.info("Creating conditional model")
    if args.condition_model is None:
        cond_model = None
    elif args.condition_model == 'class_embedder':
        from models.class_embedder import ClassEmbedder
        cond_model = ClassEmbedder(num_classes=args.num_classes,
                                   embed_dim=args.embed_dim,
                                   cond_drop_rate=args.cond_drop_rate)
    else:
        raise NotImplementedError(f"Condition model {args.condition_model} is not implemented")

    # Create Optimizer
    logger.info("Creating optimizer")
    # TODO: support faster optimizer
    trainable_params = list(var.parameters())
    if cond_model is not None:
        trainable_params += list(cond_model.parameters())
    optimizer = torch.optim.AdamW(trainable_params, lr=args.learning_rate, weight_decay=args.weight_decay)
    # Compute max_train_steps
    num_update_steps_per_epoch = math.ceil(len(dataloader) / args.gradient_accumulation_steps)
    args.max_train_steps = args.num_epochs * num_update_steps_per_epoch // accelerator.num_processes

    # Create Learning Rate Scheduler
    logger.info("Creating learning rate scheduler")
    num_warmup_steps = int(args.lr_warmup_steps *
                           args.max_train_steps) if args.lr_warmup_steps < 1.0 else int(args.lr_warmup_steps)
    lr_scheduler = get_scheduler(name=args.lr_scheduler,
                                 optimizer=optimizer,
                                 num_warmup_steps=num_warmup_steps * accelerator.num_processes,
                                 num_training_steps=args.max_train_steps)

    # Send to accelerator
    var, cond_model, vqvae, optimizer, lr_scheduler, dataloader = accelerator.prepare(
        var, cond_model, vqvae, optimizer, lr_scheduler, dataloader)

    # Start tracker
    experiment_config = vars(args)
    accelerator.init_trackers(model_name, config=experiment_config)

    # Start training
    if accelerator.is_main_process:
        logger.info("***** Training arguments *****")
        logger.info(args)
        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {len(dataset)}")
        logger.info(f"  Num Epochs = {args.num_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logger.info(f"  Total optimization steps per epoch {num_update_steps_per_epoch}")
        logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    args.completed_steps = 0
    args.starting_epoch = 0

    # TODO: add resume function
    label_condition = [834] * 4
    inference(accelerator,
              var,
              vqvae,
              cond_model,
              label_condition,
              num_samples=1,
              guidance_scale=4.0,
              top_k=900,
              top_p=0.95,
              seed=42)
    validate(var,
             vqvae,
             cond_model,
             val_dataloader,
             args,
             c_mask=args.c_mask,
             c_img=args.c_img,
             rank=0,
             guidance_scale=args.cfg,
             gibbs=args.gibbs,
             save_val=args.save_val)
    # Training
    for epoch in range(args.starting_epoch, args.num_epochs):

        args.epoch = epoch
        if accelerator.is_main_process:
            logger.info(f"Epoch {epoch+1}/{args.num_epochs}")

        # train epoch
        train_epoch(accelerator, var, vqvae, cond_model, dataloader, optimizer, lr_scheduler, progress_bar, args)

        if epoch % args.val_interval == 0:
            label_condition = [834] * 4
            inference(accelerator,
                      var,
                      vqvae,
                      cond_model,
                      label_condition,
                      num_samples=1,
                      guidance_scale=4.0,
                      top_k=900,
                      top_p=0.95,
                      seed=42)
            validate(var,
                     vqvae,
                     cond_model,
                     val_dataloader,
                     args,
                     c_mask=args.c_mask,
                     c_img=args.c_img,
                     rank=0,
                     guidance_scale=args.cfg,
                     gibbs=args.gibbs,
                     save_val=args.save_val)

            save_dir = os.path.join(args.project_dir, f"epoch_{epoch}")
            os.makedirs(save_dir, exist_ok=True)
            accelerator.save_state(save_dir)

    # end training
    accelerator.end_training()


if __name__ == '__main__':
    main()
