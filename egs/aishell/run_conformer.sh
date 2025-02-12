#!/bin/bash

# Copyright 2018-2019 Tsinghua University, Author: Keyu An
#                2020 Alex Hung
#                2022 Ziwei Li
#                  
# Apache 2.0.

# This script implements CTC-CRF training on Aishell dataset.

. ./cmd.sh ## You'll want to change cmd.sh to something that will work on your system.
           ## This relates to the queue.

. ./path.sh
# Begin configuration section.
stage=7
data=$(readlink -f data)
data_url=www.openslr.org/resources/33
aishell_wav=wav.path
aishell_trans=transcript.path
# End configuration section
. utils/parse_options.sh

if [ $stage -le 0 ]; then
  [ ! -d "$data" ] && mkdir -p $data
  local/download_and_untar.sh $data $data_url data_aishell || exit 1;
  aishell_wav=$(readlink -f $data/data_aishell/wav)
  aishell_trans=$(readlink -f $data/data_aishell/transcript)
fi

if [ $stage -le 1 ]; then
  echo "Data Preparation and FST Construction"
  # Use the same datap prepatation script from Kaldi
  local/aishell_data_prep.sh $aishell_wav $aishell_trans || exit 1;
  local/download_and_untar.sh $data $data_url resource_aishell || exit 1;
  local/aishell_prepare_phn_dict.sh || exit 1;
  # Compile the lexicon and token FSTs
  ctc-crf/ctc_compile_dict_token.sh --dict-type "phn" \
    data/local/dict_phn data/local/lang_phn_tmp data/lang_phn || exit 1;
  # Train and compile LMs.
  local/aishell_train_lms.sh data/local/train/text data/local/dict_phn/lexicon.txt data/local/lm_phn || exit 1;
  # Compile the language-model FST and the final decoding graph TLG.fst
  local/aishell_decode_graph.sh data/local/lm_phn data/lang_phn data/lang_phn_test || exit 1;
fi

##############################################################################################################

if [ $stage -le 2 ]; then
  echo "FBank Feature Generation"
  utils/data/perturb_data_dir_speed_3way.sh data/train data/train_sp
  utils/data/perturb_data_dir_speed_3way.sh data/dev data/dev_sp
  echo " preparing directory for speed-perturbed data done"

  # Generate the fbank features; by default 40-dimensional fbanks on each frame
  fbankdir=fbank
  for set in train_sp dev_sp; do
    steps/make_fbank.sh --cmd "$train_cmd" --nj 20 data/$set exp/make_fbank/$set $fbankdir || exit 1;
    utils/fix_data_dir.sh data/$set || exit;
    steps/compute_cmvn_stats.sh data/$set exp/make_fbank/$set $fbankdir || exit 1;
  done

  for set in test; do
    steps/make_fbank.sh --cmd "$train_cmd" --nj 20 data/$set exp/make_fbank/$set $fbankdir || exit 1;
    utils/fix_data_dir.sh data/$set || exit;
    steps/compute_cmvn_stats.sh data/$set exp/make_fbank/$set $fbankdir || exit 1;
  done
fi

data_tr=data/train_sp
data_cv=data/dev_sp

if [ $stage -le 3 ]; then
  ctc-crf/prep_ctc_trans.py data/lang_phn/lexicon_numbers.txt $data_tr/text "<UNK>" > $data_tr/text_number
  ctc-crf/prep_ctc_trans.py data/lang_phn/lexicon_numbers.txt $data_cv/text "<UNK>" > $data_cv/text_number
  echo "convert text_number finished"

  # prepare denominator
  ctc-crf/prep_ctc_trans.py data/lang_phn/lexicon_numbers.txt data/train/text "<UNK>" > data/train/text_number
  cat data/train/text_number | sort -k 2 | uniq -f 1 > data/train/unique_text_number
  mkdir -p data/den_meta
  chain-est-phone-lm ark:data/train/unique_text_number data/den_meta/phone_lm.fst
  ctc-crf/ctc_token_fst_corrected.py den data/lang_phn/tokens.txt | fstcompile | fstarcsort --sort_type=olabel > data/den_meta/T_den.fst
  fstcompose data/den_meta/T_den.fst data/den_meta/phone_lm.fst > data/den_meta/den_lm.fst
  echo "prepare denominator finished"
fi

if [ $stage -le 4 ]; then
  path_weight $data_tr/text_number data/den_meta/phone_lm.fst > $data_tr/weight
  path_weight $data_cv/text_number data/den_meta/phone_lm.fst > $data_cv/weight
  echo "prepare weight finished"

  feats_tr="ark,s,cs:apply-cmvn --norm-vars=true --utt2spk=ark:$data_tr/utt2spk scp:$data_tr/cmvn.scp scp:$data_tr/feats.scp ark:- |"
  #echo "$feats_tr"
  feats_cv="ark,s,cs:apply-cmvn --norm-vars=true --utt2spk=ark:$data_cv/utt2spk scp:$data_cv/cmvn.scp scp:$data_cv/feats.scp ark:- |"

  ark_dir=data/all_ark
  mkdir -p $ark_dir
  copy-feats "$feats_tr" "ark,scp:$ark_dir/tr.ark,$ark_dir/tr.scp" || exit 1
  copy-feats "$feats_cv" "ark,scp:$ark_dir/cv.ark,$ark_dir/cv.scp" || exit 1

  echo "copy -feats finished"
  ark_dir=data/all_ark
  #mkdir -p data/hdf5
  #python3 ctc-crf/convert_to.py $ark_dir/cv.scp $data_cv/text_number $data_cv/weight data/hdf5/cv.hdf5
  #python3 ctc-crf/convert_to.py $ark_dir/tr.scp $data_tr/text_number $data_tr/weight data/hdf5/tr.hdf5

  mkdir -p data/pickle
  python3 ctc-crf/convert_to.py $ark_dir/cv.scp $data_cv/text_number $data_cv/weight data/pickle/cv.pickle
  python3 ctc-crf/convert_to.py $ark_dir/tr.scp $data_tr/text_number $data_tr/weight data/pickle/tr.pickle
fi

data_test=data/test

if [ $stage -le 5 ]; then
  feats_test="ark,s,cs:apply-cmvn --norm-vars=true --utt2spk=ark:$data_test/utt2spk scp:$data_test/cmvn.scp scp:$data_test/feats.scp ark:- |"
  mkdir -p data/test_data
  copy-feats "$feats_test" "ark,scp:data/test_data/test.ark,data/test_data/test.scp"
fi

PARENTDIR='.'
dir="exp/conformer"
DATAPATH=$PARENTDIR/data/

NODE=$1
if [ ! $NODE ]
then
    NODE=0
fi

if [ $stage -le 6 ]; then
unset CUDA_VISIBLE_DEVICES

if [[ $NODE == 0 && ! -f $dir/scripts.tar.gz ]]
then
    echo ""
    tar -zcf $dir/scripts.tar.gz $(readlink ctc-crf) $0
elif [ $NODE == 0 ]
then
    echo ""
    echo "'$dir/scripts.tar.gz' already exists."
    echo "If you want to update it, please manually rm it then re-run this script."
fi


python3 ctc-crf/train.py --seed=0            \
    --world-size 1 --rank $NODE                 \
    --batch_size=64                           \
    --print-freq=1                             \
    --grad-accum-fold=2                         \
    --dir=$dir                                  \
    --config=$dir/config.json                   \
    --data=$DATAPATH                            \
    || exit 1
fi

if [ $NODE -ne 0 ]
then
  exit 0
fi

if [ $stage -le 7 ]; then
  for set in test; do
    ark_dir=$dir/logits/${set}
    mkdir -p $ark_dir
    CUDA_VISIBLE_DEVICES=0 python3 ctc-crf/calculate_logits.py \
      --resume=$dir/ckpt/bestckpt.pt                      \
      --config=$dir/config.json                     \
      --nj=20 --input_scp=data/test_data/${set}.scp \
      --output_dir=$ark_dir || exit 1
 done
fi

if [ $stage -le 8 ]; then
  nj=20
  for set in test; do
     mkdir -p $dir/decode_${set}
     ln -s $(readlink -f $dir/logits/${set}) $dir/decode_${set}/logits
     ctc-crf/decode.sh --stage 1 \
     --cmd "$decode_cmd" --nj $nj --acwt 1.0 \
     data/lang_phn_test data/test data/test_data/${set}.scp $dir/decode_${set}
  done
fi

grep WER $dir/decode_test/cer_* | utils/best_wer.sh
grep WER $dir/decode_test/wer_* | utils/best_wer.sh

