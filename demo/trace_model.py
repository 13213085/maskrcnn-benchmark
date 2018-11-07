# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import os
import argparse
from typing import List, Tuple
import time

import cv2
import numpy
from matplotlib import pyplot

import torch

from PIL import Image
from maskrcnn_benchmark.config import cfg
from predictor import COCODemo
from maskrcnn_benchmark.structures.image_list import to_image_list, ImageList
from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.modeling.box_coder import BoxCoder
from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist
from maskrcnn_benchmark.structures.boxlist_ops import boxlist_nms
from maskrcnn_benchmark.structures.boxlist_ops import remove_small_boxes
import maskrcnn_benchmark.layers



def single_image_to_top_predictions(image):
    # we are loading images with OpenCV, so we don't need to convert them
    # to BGR, they are already! So all we need to do is to normalize
    # by 255 if we want to convert to BGR255 format, or flip the channels
    # if we want it to be in RGB in [0-1] range.
    if cfg.INPUT.TO_BGR255:
        image = image * 255
    else:
        image = image[[2, 1, 0]]

    # we absolutely want fixed size (int) here (or we run into a tracing error (or bug?)
    # or we might later decide to make things work with variable size...
    image = image - torch.tensor(cfg.INPUT.PIXEL_MEAN)[:, None, None]
    # should also do variance...
    image_list = ImageList(image.unsqueeze(0), [(int(image.size(-2)), int(image.size(-1)))])
    result, = coco_demo.model.forward(image_list)
    scores = result.get_field("scores")
    keep = (scores >= coco_demo.confidence_threshold)
    result = (result.bbox[keep],
              result.get_field("labels")[keep],
              result.get_field("mask")[keep],
              scores[keep])
    return result

@torch.jit.script
def my_paste_mask(mask, bbox, height: int, width: int, threshold: float=0.5, padding: int=1, contour: bool=True, rectangle: bool=False):
    padded_mask = torch.constant_pad_nd(mask, (padding, padding, padding, padding))
    scale = 1.0 + 2.0 * float(padding) / float(mask.size(-1))
    center_x = (bbox[2] + bbox[0]) * 0.5
    center_y = (bbox[3] + bbox[1]) * 0.5
    w_2      = (bbox[2] - bbox[0]) * 0.5 * scale
    h_2      = (bbox[3] - bbox[1]) * 0.5 * scale # should have two scales?
    bbox_scaled = torch.stack([center_x - w_2, center_y - h_2,
                               center_x + w_2, center_y + h_2], 0)

    TO_REMOVE = 1
    w = (bbox_scaled[2] - bbox_scaled[0] + TO_REMOVE).clamp(min=1).long()
    h = (bbox_scaled[3] - bbox_scaled[1] + TO_REMOVE).clamp(min=1).long()

    scaled_mask = torch.ops.maskrcnn_benchmark.upsample_bilinear(padded_mask.float(), h, w)

    x0 = bbox_scaled[0].long()
    y0 = bbox_scaled[1].long()
    x = x0.clamp(min=0)
    y = y0.clamp(min=0)
    leftcrop = x - x0
    topcrop  = y - y0
    w = torch.min(w - leftcrop, width - x)
    h = torch.min(h - topcrop, height - y)

    # mask = torch.zeros((height, width), dtype=torch.uint8)
    # mask[y:y + h, x:x + w] = (scaled_mask[topcrop:topcrop + h,  leftcrop:leftcrop + w] > threshold)
    mask = torch.constant_pad_nd((scaled_mask[topcrop:topcrop + h,  leftcrop:leftcrop + w] > threshold),
                                 (int(x), int(width - x - w), int(y), int(height - y - h)))   # int for the script compiler
    
    if contour:
        mask = mask.float()
        # poor person's contour finding by comparing to smoothed
        mask = (mask - torch.nn.functional.conv2d(mask.unsqueeze(0).unsqueeze(0),
                                                  torch.full((1,1,3,3), 1.0/9.0), padding=1)[0, 0]).abs()>0.001
    if rectangle:
        x = torch.arange(width, dtype=torch.long).unsqueeze(0)
        y = torch.arange(height, dtype=torch.long).unsqueeze(1)
        r = bbox.long()
        # work around script not liking bitwise ops
        rectangle_mask = (  (((x==r[0]) + (x==r[2])) * (y>=r[1]) * (y<=r[3]))
                          + (((y==r[1]) + (y==r[3])) * (x>=r[0]) * (x<=r[2])))
        mask = (mask + rectangle_mask).clamp(max=1)
    return mask

@torch.jit.script
def combine_masks(image, labels, masks, bboxes, threshold: float=0.5, padding: int=1, contour: bool=True, rectangle: bool=False, palette=torch.tensor([33554431, 32767, 2097151])):
    """note: image is C,H,W!"""
    height = image.size(-2)
    width = image.size(-1)
    image_with_mask = image.clone()
    for i in range(masks.size(0)):
        color = ((palette * labels[i]) % 255).to(torch.uint8)
        one_mask = my_paste_mask(masks[i, 0], bboxes[i], height, width, threshold, padding, contour, rectangle)
        image_with_mask = torch.where(one_mask.unsqueeze(0), color.unsqueeze(1).unsqueeze(1), image_with_mask)
    return image_with_mask

def process_image_with_traced_model(image):
    original_image = image
    image = image.float()/255.0

    if coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY:
        assert (image.size(-2) % coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY == 0
                and image.size(-1) % coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY == 0)

    boxes, labels, masks, scores  = traced_model(image)

    # todo: make this in one large thing
    result_image = combine_masks(original_image, labels, masks, boxes, 0.5, 1, rectangle=True)
    result_image = result_image.permute(1,2,0)
    template = "{}: {:.2f}"
    for i in range(len(boxes)):
        s = template.format(coco_demo.CATEGORIES[labels[i].item()], scores[i].item())
        result_image = torch.ops.maskrcnn_benchmark.put_text(result_image, int(boxes[i, 0]), int(boxes[i, 1]),
                                                             torch.tensor([255, 255, 255], dtype=torch.long), s)

    return result_image


if __name__ == "__main__":
    # load config from file and command-line arguments
    cfg.merge_from_file("../configs/caffe2/e2e_mask_rcnn_R_50_FPN_1x_caffe2.yaml")
    cfg.merge_from_list(["MODEL.DEVICE", "cpu"])
    cfg.freeze()

    # prepare object that handles inference plus adds predictions on top of image
    coco_demo = COCODemo(
        cfg,
        confidence_threshold=0.7,
        show_mask_heatmaps=False,
        masks_per_dim=2,
        min_image_size=480,
    )

    pil_image = Image.open("3915380994_2e611b1779_z.jpg").convert("RGB")
    # convert to BGR format
    image = torch.from_numpy(numpy.array(pil_image)[:, :, [2, 1, 0]])
    image = image.permute(2, 0, 1)
    original_image = image
    image = image.float()/255.0

    if coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY:
        assert (image.size(-2) % coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY == 0
                and image.size(-1) % coco_demo.cfg.DATALOADER.SIZE_DIVISIBILITY == 0)

    with torch.no_grad():
        traced_model = torch.jit.trace(single_image_to_top_predictions, (image,))


    result_image = process_image_with_traced_model(original_image)
    
    # self.show_mask_heatmaps not done
    pyplot.imshow(result_image[:, :, [2,1,0]])
    pyplot.show()
        


    # second image
    image2 = Image.open('17790319373_bd19b24cfc_k.jpg').convert("RGB")
    image2 = image2.resize((640, 480), Image.BILINEAR)
    image2 = torch.from_numpy(numpy.array(image2)[:, :, [2, 1, 0]])
    image2 = image2.permute(2, 0, 1)
    result_image2 = process_image_with_traced_model(image2)
    
    # self.show_mask_heatmaps not done
    pyplot.imshow(result_image2[:, :, [2,1,0]])
    pyplot.show()
