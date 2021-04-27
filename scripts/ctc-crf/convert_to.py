"""
Copyright 2021 Tsinghua University
Apache 2.0.
Author: Hongyu Xiang, Keyu An, Zheng Huahuan
"""

import kaldi_io
import numpy as np
import argparse
import utils
import pickle
import h5py
from tqdm import tqdm


def ctc_len(label):
    extra = 0
    for i in range(len(label)-1):
        if label[i] == label[i+1]:
            extra += 1
    return len(label) + extra


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="convert to pickle")
    parser.add_argument("-f", "--format", type=str,
                        choices=["hdf5", "pickle"], default="pickle")
    parser.add_argument("-W", "--warning", action="store_true",
                        default=False)
    parser.add_argument("scp", type=str)
    parser.add_argument("label", type=str)
    parser.add_argument("weight", type=str)
    parser.add_argument("output_path", type=str)

    args = parser.parse_args()

    if args.warning:
        utils.highlight_msg(
            "Calculation of CTC loss requires the input sequence to be longer than ctc_len(labels).\nCheck that in 'ctc-crf/convert_to.py' if your model does subsampling on seq.\nMake your modify at line 'if feature.shape[0] < ctc_len(label):' to filter unqualified seq.\nIf you have already done, ignore this.")

    label_dict = {}
    with open(args.label, 'r') as fi:
        lines = fi.readlines()
        for line in lines:
            sp = line.split()
            label_dict[sp[0]] = np.asarray([int(x) for x in sp[1:]])

    weight_dict = {}
    with open(args.weight, 'r') as fi:
        lines = fi.readlines()
        for line in lines:
            sp = line.split()
            weight_dict[sp[0]] = np.asarray([float(sp[1])])

    if args.format == "hdf5":
        h5_file = h5py.File(args.output_path, 'w')
    else:
        pickle_dataset = []

    with open(args.scp, 'r') as fi:
        lines = fi.readlines()
        for line in tqdm(lines):
            key, value = line.split()

            label = label_dict[key]
            weight = weight_dict[key]
            feature = kaldi_io.read_mat(value)
            feature = np.asarray(feature)

            if feature.shape[0] < ctc_len(label):
                continue

            if args.format == "hdf5":
                dset = h5_file.create_dataset(key, data=feature)
                dset.attrs['label'] = label
                dset.attrs['weight'] = weight
            else:
                pickle_dataset.append([key, value, label, weight])

    if args.format == "pickle":
        with open(args.output_path, 'wb') as fo:
            pickle.dump(pickle_dataset, fo)
