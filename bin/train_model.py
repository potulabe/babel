"""
Code to train a model
"""

import argparse
import copy
import itertools
import logging
import os

import matplotlib.pyplot as plt
import numpy as np
import scanpy as sc
import skorch
import skorch.helper
import torch
import torch.nn as nn

import babel_my.activations as activations
import babel_my.loss_functions as loss_functions
import babel_my.model_utils as model_utils
import babel_my.models.autoencoders as autoencoders
import babel_my.plot_utils as plot_utils
import babel_my.sc_data_loaders as sc_data_loaders
import babel_my.utils as utils
from babel_my.metrics import eval_correlation

torch.backends.cudnn.deterministic = True  # For reproducibility
torch.backends.cudnn.benchmark = False

# SRC_DIR = os.path.join(
#     os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "babel"
# )
# assert os.path.isdir(SRC_DIR)
# sys.path.append(SRC_DIR)

# MODELS_DIR = os.path.join(SRC_DIR, "models")
# assert os.path.isdir(MODELS_DIR)
# sys.path.append(MODELS_DIR)


logging.basicConfig(level=logging.INFO)

OPTIMIZER_DICT = {
    "adam": torch.optim.Adam,
    "rmsprop": torch.optim.RMSprop,
}


def build_parser():
    """Build argument parser"""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # input_group = parser.add_mutually_exclusive_group(required=True)
    # input_group.add_argument(
    #     "--data", "-d", type=str, nargs="*", help="Data files to train on",
    # )
    parser.add_argument(
        "--data-RNA", "-dr", type=str, help="RNA data files to train on", required=True
    )
    parser.add_argument(
        "--data-ATAC", "-da", type=str, help="ATAC data files to train on", required=True
    )
    parser.add_argument(
        "--rna-normalised",
        action="store_true",
        help="If RNA-seq data is already normalised",
    )
    parser.add_argument(
        "--atac-normalised",
        action="store_true",
        help="If ATAC-seq data is already normalised",
    )
    parser.add_argument(
        "--snareseq",
        action="store_true",
        help="Data in SNAREseq format, use custom data loading logic for separated RNA ATC files",
    )
    parser.add_argument(
        "--shareseq",
        nargs="+",
        type=str,
        choices=["lung", "skin", "brain"],
        help="Load in the given SHAREseq datasets",
    )
    parser.add_argument(
        "--nofilter",
        action="store_true",
        help="Whether or not to perform filtering (only applies with --data argument)",
    )
    parser.add_argument(
        "--linear",
        action="store_true",
        help="Do clustering data splitting in linear instead of log space",
    )
    parser.add_argument(
        "--clustermethod",
        type=str,
        choices=["leiden", "louvain"],
        default=None,
        help="Clustering method to determine data splits",
    )
    parser.add_argument(
        "--validcluster", type=int, default=0, help="Cluster ID to use as valid cluster"
    )
    parser.add_argument(
        "--testcluster", type=int, default=1, help="Cluster ID to use as test cluster"
    )
    parser.add_argument(
        "--outdir", "-o", required=True, type=str, help="Directory to output to"
    )
    parser.add_argument(
        "--naive",
        "-n",
        action="store_true",
        help="Use a naive model instead of lego model",
    )
    parser.add_argument(
        "--hidden", type=int, nargs="*", default=[16], help="Hidden dimensions"
    )
    parser.add_argument(
        "--pretrain",
        type=str,
        default="",
        help="params.pt file to use to warm initialize the model (instead of starting from scratch)",
    )
    parser.add_argument(
        "--lossweight",
        type=float,
        nargs="*",
        default=[1.33],
        help="Relative loss weight",
    )
    parser.add_argument(
        "--optim",
        type=str,
        default="adam",
        choices=OPTIMIZER_DICT.keys(),
        help="Optimizer to use",
    )
    parser.add_argument(
        "--lr", "-l", type=float, default=[0.01], nargs="*", help="Learning rate"
    )
    parser.add_argument(
        "--batchsize", "-b", type=int, nargs="*", default=[512], help="Batch size"
    )
    parser.add_argument(
        "--earlystop", type=int, default=25, help="Early stopping after N epochs"
    )
    parser.add_argument(
        "--seed", type=int, nargs="*", default=[182822], help="Random seed to use"
    )
    parser.add_argument("--device", default=0, type=int, help="Device to train on")
    parser.add_argument(
        "--ext",
        type=str,
        choices=["png", "pdf", "jpg"],
        default="pdf",
        help="Output format for plots",
    )
    return parser


def plot_loss_history(history, fname: str):
    """Create a plot of train valid loss"""
    fig, ax = plt.subplots(dpi=300)
    ax.plot(
        np.arange(len(history)), history[:, "train_loss"], label="Train",
    )
    ax.plot(
        np.arange(len(history)), history[:, "valid_loss"], label="Valid",
    )
    ax.legend()
    ax.set(
        xlabel="Epoch", ylabel="Loss",
    )
    fig.savefig(fname)
    return fig


def main():
    """Run the script"""
    parser = build_parser()
    args = parser.parse_args()
    args.outdir = os.path.abspath(args.outdir)

    if not os.path.isdir(os.path.dirname(args.outdir)):
        os.makedirs(os.path.dirname(args.outdir))

    # Specify output log file
    logger = logging.getLogger()
    fh = logging.FileHandler(f"{args.outdir}_training.log", "w")
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)
    for arg in vars(args):
        logging.info(f"Parameter {arg}: {getattr(args, arg)}")
    do_train_model(
            args.data_RNA,
            args.data_ATAC,
            args.nofilter,
            args.linear,
            args.clustermethod,
            args.rna_normalised,
            args.atac_normalised,
            args.validcluster,
            args.testcluster,
            args.hidden, args.lossweight, args.lr, args.batchsize,
            args.earlystop,
            args.seed,
            args.outdir,
            args.naive,
            args.optim,
            args.device,
            args.pretrain,
            args.ext
    )


def do_train_model(
        data_RNA,
        data_ATAC,
        nofilter,
        linear,
        clustermethod,
        rna_normalised,
        atac_normalised,
        validcluster,
        testcluster,
        hidden, lossweight, lr, batchsize,
        earlystop,
        seed,
        outdir,
        naive,
        optim,
        device,
        pretrain,
        ext
):
    # Log parameters and pytorch version
    if torch.cuda.is_available():
        logging.info(f"PyTorch CUDA version: {torch.version.cuda}")

    # Borrow parameters
    logging.info("Reading RNA data")
    rna_data_kwargs = copy.copy(sc_data_loaders.TENX_PBMC_RNA_DATA_KWARGS)
    rna_data = utils.read_adata(data_RNA)
    rna_data_kwargs["raw_adata"] = rna_data
    if nofilter:
        rna_data_kwargs = {
            k: v for k, v in rna_data_kwargs.items() if not k.startswith("filt_")
        }
    rna_data_kwargs["data_split_by_cluster_log"] = not linear
    rna_data_kwargs["data_split_by_cluster"] = clustermethod  # TODO
    rna_data_kwargs["normalised"] = rna_normalised

    sc_rna_dataset = sc_data_loaders.SingleCellDataset(
        valid_cluster_id=validcluster,  # TODO
        test_cluster_id=testcluster,  # TODO
        **rna_data_kwargs,
    )

    sc_rna_train_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_rna_dataset, split="train",
    )
    sc_rna_valid_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_rna_dataset, split="valid",
    )
    sc_rna_test_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_rna_dataset, split="test",
    )

    # ATAC
    # logging.info("Aggregating ATAC clusters")
    
    # atac_parsed = [
    #     utils.sc_read_10x_h5_ft_type(fname, "Peaks") for fname in args.data
    # ]
    # if len(atac_parsed) > 1:
    #     atac_bins = sc_data_loaders.harmonize_atac_intervals(
    #         atac_parsed[0].var_names, atac_parsed[1].var_names
    #     )
    #     for bins in atac_parsed[2:]:
    #         atac_bins = sc_data_loaders.harmonize_atac_intervals(
    #             atac_bins, bins.var_names
    #         )
    #     logging.info(f"Aggregated {len(atac_bins)} bins")
    # else:
    #     atac_bins = list(atac_parsed[0].var_names)
    atac_data = utils.read_adata(data_ATAC)

    atac_data_kwargs = copy.copy(sc_data_loaders.TENX_PBMC_ATAC_DATA_KWARGS)
    # atac_data_kwargs["fname"] = rna_data_kwargs["fname"]
    atac_data_kwargs["raw_adata"] = atac_data
    atac_data_kwargs["pool_genomic_interval"] = 0  # Do not pool
    # atac_data_kwargs["reader"] = functools.partial(
    #     utils.sc_read_multi_files,
    #     reader=lambda x: sc_data_loaders.repool_atac_bins(
    #         utils.sc_read_10x_h5_ft_type(x, "Peaks"), atac_bins,
    #     ),
    # )
    atac_data_kwargs["cluster_res"] = 0  # Do not bother clustering ATAC data
    atac_data_kwargs["normalised"] = atac_normalised

    sc_atac_dataset = sc_data_loaders.SingleCellDataset(
        predefined_split=sc_rna_dataset, **atac_data_kwargs
    )
    sc_atac_train_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_atac_dataset, split="train",
    )
    sc_atac_valid_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_atac_dataset, split="valid",
    )
    sc_atac_test_dataset = sc_data_loaders.SingleCellDatasetSplit(
        sc_atac_dataset, split="test",
    )

    sc_dual_train_dataset = sc_data_loaders.PairedDataset(
        sc_rna_train_dataset, sc_atac_train_dataset, flat_mode=True,
    )
    sc_dual_valid_dataset = sc_data_loaders.PairedDataset(
        sc_rna_valid_dataset, sc_atac_valid_dataset, flat_mode=True,
    )
    sc_dual_test_dataset = sc_data_loaders.PairedDataset(
        sc_rna_test_dataset, sc_atac_test_dataset, flat_mode=True,
    )
    # sc_dual_full_dataset = sc_data_loaders.PairedDataset(
    #     sc_rna_dataset, sc_atac_dataset, flat_mode=True,
    # )

    # Model
    param_combos = list(
        itertools.product(
            hidden, lossweight, lr, batchsize, seed
        )
    )
    for h_dim, lw, lr, bs, rand_seed in param_combos:
        outdir_name = (
            f"{outdir}_hidden_{h_dim}_lossweight_{lw}_lr_{lr}_batchsize_{bs}_seed_{rand_seed}"
            if len(param_combos) > 1
            else outdir
        )
        if not os.path.isdir(outdir_name):
            assert not os.path.exists(outdir_name)
            os.makedirs(outdir_name)
        assert os.path.isdir(outdir_name)
        with open(os.path.join(outdir_name, "rna_genes.txt"), "w") as sink:
            for gene in sc_rna_dataset.data_raw.var_names:
                sink.write(gene + "\n")
        with open(os.path.join(outdir_name, "atac_bins.txt"), "w") as sink:
            for atac_bin in sc_atac_dataset.data_raw.var_names:
                sink.write(atac_bin + "\n")

        # Write dataset
        ### Full
        # sc_rna_dataset.size_norm_counts.write_h5ad(
        #     os.path.join(outdir_name, "full_rna.h5ad")
        # )
        # sc_rna_dataset.size_norm_log_counts.write_h5ad(
        #     os.path.join(outdir_name, "full_rna_log.h5ad")
        # )
        # sc_atac_dataset.data_raw.write_h5ad(os.path.join(outdir_name, "full_atac.h5ad"))
        # ### Train
        # sc_rna_train_dataset.size_norm_counts.write_h5ad(
        #     os.path.join(outdir_name, "train_rna.h5ad")
        # )
        # sc_atac_train_dataset.data_raw.write_h5ad(
        #     os.path.join(outdir_name, "train_atac.h5ad")
        # )
        # ### Valid
        # sc_rna_valid_dataset.size_norm_counts.write_h5ad(
        #     os.path.join(outdir_name, "valid_rna.h5ad")
        # )
        # sc_atac_valid_dataset.data_raw.write_h5ad(
        #     os.path.join(outdir_name, "valid_atac.h5ad")
        # )
        # ### Test
        # sc_rna_test_dataset.size_norm_counts.write_h5ad(
        #     os.path.join(outdir_name, "truth_rna.h5ad")
        # )
        # sc_atac_dataset.data_raw.write_h5ad(os.path.join(outdir_name, "full_atac.h5ad"))
        # sc_atac_test_dataset.data_raw.write_h5ad(
        #     os.path.join(outdir_name, "truth_atac.h5ad")
        # )

        # Instantiate and train model
        model_class = (
            autoencoders.NaiveSplicedAutoEncoder
            if naive
            else autoencoders.AssymSplicedAutoEncoder
        )
        spliced_net = autoencoders.SplicedAutoEncoderSkorchNet(
            module=model_class,
            module__hidden_dim=h_dim,  # Based on hyperparam tuning
            module__input_dim1=sc_rna_dataset.data_raw.shape[1],
            module__input_dim2=sc_atac_dataset.get_per_chrom_feature_count(),
            module__final_activations1=[
                activations.Exp(),
                activations.ClippedSoftplus(),
            ],
            module__final_activations2=nn.Sigmoid(),
            module__flat_mode=True,
            module__seed=rand_seed,
            lr=lr,  # Based on hyperparam tuning
            criterion=loss_functions.QuadLoss,
            criterion__loss2=loss_functions.BCELoss,  # handle output of encoded layer
            criterion__loss2_weight=lw,  # numerically balance the two losses with different magnitudes
            criterion__record_history=True,
            optimizer=OPTIMIZER_DICT[optim],
            iterator_train__shuffle=True,
            device=utils.get_device(device),
            batch_size=bs,  # Based on  hyperparam tuning
            max_epochs=500,
            callbacks=[
                skorch.callbacks.EarlyStopping(patience=earlystop),
                skorch.callbacks.LRScheduler(
                    policy=torch.optim.lr_scheduler.ReduceLROnPlateau,
                    **model_utils.REDUCE_LR_ON_PLATEAU_PARAMS,
                ),
                skorch.callbacks.GradientNormClipping(gradient_clip_value=5),
                skorch.callbacks.Checkpoint(
                    dirname=outdir_name, fn_prefix="net_", monitor="valid_loss_best",
                ),
            ],
            train_split=skorch.helper.predefined_split(sc_dual_valid_dataset),
            iterator_train__num_workers=8,
            iterator_valid__num_workers=8,
        )
        if pretrain:
            # Load in the warm start parameters
            spliced_net.load_params(f_params=pretrain)
            spliced_net.partial_fit(sc_dual_train_dataset, y=None)
        else:
            spliced_net.fit(sc_dual_train_dataset, y=None)

        fig = plot_loss_history(
            spliced_net.history, os.path.join(outdir_name, f"loss.{ext}")
        )
        plt.close(fig)

        logging.info("Evaluating on test set")
        logging.info("Evaluating RNA > RNA")
        sc_rna_test_preds = spliced_net.translate_1_to_1(sc_dual_test_dataset)
        sc_rna_test_preds_anndata = sc.AnnData(
            sc_rna_test_preds,
            var=sc_rna_test_dataset.data_raw.var,
            obs=sc_rna_test_dataset.data_raw.obs,
        )
        rna_rna_cors = eval_correlation(
                sc_rna_test_preds_anndata, sc_rna_test_dataset.data_raw
        )

        sc_rna_test_preds_anndata.write_h5ad(
            os.path.join(outdir_name, "rna_rna_test_preds.h5ad")
        )
        fig = plot_utils.plot_scatter_with_r(
            sc_rna_test_dataset.data_raw.X,
            sc_rna_test_preds,
            one_to_one=True,
            logscale=True,
            density_heatmap=True,
            title=f"RNA > RNA (test set), mean Pearson cor = {rna_rna_cors.mean(): .4f}",
            fname=os.path.join(outdir_name, f"rna_rna_scatter_log.{ext}"),
        )
        plt.close(fig)

        logging.info("Evaluating ATAC > ATAC")
        sc_atac_test_preds = spliced_net.translate_2_to_2(sc_dual_test_dataset)
        sc_atac_test_preds_anndata = sc.AnnData(
            sc_atac_test_preds,
            var=sc_atac_test_dataset.data_raw.var,
            obs=sc_atac_test_dataset.data_raw.obs,
        )
        atac_atac_cors = eval_correlation(
                sc_atac_test_preds_anndata, sc_atac_test_dataset.data_raw
        )
        sc_atac_test_preds_anndata.write_h5ad(
            os.path.join(outdir_name, "atac_atac_test_preds.h5ad")
        )
        fig = plot_utils.plot_auroc(
            sc_atac_test_dataset.data_raw.X,
            sc_atac_test_preds,
            title_prefix=f"ATAC > ATAC, mean Pearson cor = {atac_atac_cors.mean(): .4f}",
            fname=os.path.join(outdir_name, f"atac_atac_auroc.{ext}"),
        )
        plt.close(fig)

        logging.info("Evaluating ATAC > RNA")
        sc_atac_rna_test_preds = spliced_net.translate_2_to_1(sc_dual_test_dataset)
        sc_atac_rna_test_preds_anndata = sc.AnnData(
            sc_atac_rna_test_preds,
            var=sc_rna_test_dataset.data_raw.var,
            obs=sc_rna_test_dataset.data_raw.obs,
        )
        atac_rna_cors = eval_correlation(
                sc_atac_rna_test_preds_anndata, sc_rna_test_dataset.data_raw
        )
        sc_atac_rna_test_preds_anndata.write_h5ad(
            os.path.join(outdir_name, "atac_rna_test_preds.h5ad")
        )
        fig = plot_utils.plot_scatter_with_r(
            sc_rna_test_dataset.data_raw.X,
            sc_atac_rna_test_preds,
            one_to_one=True,
            logscale=True,
            density_heatmap=True,
            title=f"ATAC > RNA (test set), mean Pearson cor = {atac_rna_cors.mean(): .4f}",
            fname=os.path.join(outdir_name, f"atac_rna_scatter_log.{ext}"),
        )
        plt.close(fig)

        logging.info("Evaluating RNA > ATAC")
        sc_rna_atac_test_preds = spliced_net.translate_1_to_2(sc_dual_test_dataset)
        sc_rna_atac_test_preds_anndata = sc.AnnData(
            sc_rna_atac_test_preds,
            var=sc_atac_test_dataset.data_raw.var,
            obs=sc_atac_test_dataset.data_raw.obs,
        )
        rna_atac_cors = eval_correlation(
                sc_rna_atac_test_preds_anndata, sc_atac_test_dataset.data_raw
        )
        sc_rna_atac_test_preds_anndata.write_h5ad(
            os.path.join(outdir_name, "rna_atac_test_preds.h5ad")
        )
        fig = plot_utils.plot_auroc(
            sc_atac_test_dataset.data_raw.X,
            sc_rna_atac_test_preds,
            title_prefix=f"RNA > ATAC, mean Pearson cor = {rna_atac_cors.mean(): .4f}",
            fname=os.path.join(outdir_name, f"rna_atac_auroc.{ext}"),
        )
        plt.close(fig)

        del spliced_net


if __name__ == "__main__":
    main()
