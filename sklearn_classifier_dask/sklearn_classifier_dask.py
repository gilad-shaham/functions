import mlrun

import warnings

warnings.filterwarnings("ignore")

import os
import joblib
import numpy as np
import pandas as pd
from cloudpickle import dumps, load, dump

from dask import dataframe as dd
from dask import array as da
from dask.delayed import delayed
from dask_ml import model_selection
from dask_ml import metrics
from dask_ml.preprocessing import StandardScaler, LabelEncoder

from mlrun.artifacts import PlotArtifact
from mlrun.mlutils.models import gen_sklearn_model
from mlrun.utils.helpers import create_class

import matplotlib.pyplot as plt
from yellowbrick.classifier import ROCAUC, ClassificationReport, ConfusionMatrix
from yellowbrick.model_selection import FeatureImportances


def train_model(
    context,
    dataset: mlrun.DataItem,
    model_pkg_class: str,
    label_column: str = "label",
    train_validation_size: float = 0.75,
    sample: float = 1.0,
    models_dest: str = "models",
    test_set_key: str = "test_set",
    plots_dest: str = "plots",
    dask_function: str = None,
    dask_client=None,
    file_ext: str = "parquet",
    random_state: int = 42,
) -> None:

    """
    Train a sklearn classifier with Dask

    :param context:                 Function context.
    :param dataset:                 Raw data file.
    :param model_pkg_class:         Model to train, e.g, "sklearn.ensemble.RandomForestClassifier",
                                    or json model config.
    :param label_column:            (label) Ground-truth y labels.
    :param train_validation_size:   (0.75) Train validation set proportion out of the full dataset.
    :param sample:                  (1.0) Select sample from dataset (n-rows/% of total), randomzie rows as default.
    :param models_dest:             (models) Models subfolder on artifact path.
    :param test_set_key:            (test_set) Mlrun db key of held out data in artifact store.
    :param plots_dest:              (plots) Plot subfolder on artifact path.
    :param dask_function:           dask function url (db://..)
    :param dask_client:             dask client object
    :param file_ext:                (parquet) format for test_set_key hold out data
    :param random_state:            (42) sklearn seed
    """

    # set up dask client
    if dask_function:
        client = mlrun.import_function(dask_function).client
    elif dask_client:
        client = dask_client
    else:
        raise ValueError("dask client was not provided")

    context.logger.info("Read Data")
    # read data with dask and mlrun
    df = dataset.as_df(df_module=dd)

    # take only numrical cols
    context.logger.info("Prep Data")
    numerics = ["int16", "int32", "int64", "float16", "float32", "float64"]
    df = df.select_dtypes(include=numerics)

    # dropna
    if df.isna().any().any().compute() == True:
        raise Exception("NAs valus found")

    # save cols names
    df_header = df.columns

    df = df.sample(frac=sample).reset_index(drop=True)
    encoder = LabelEncoder()
    encoder = encoder.fit(df[label_column])
    X = df.drop(label_column, axis=1).to_dask_array(lengths=True)
    y = encoder.transform(df[label_column])

    classes = df[label_column].drop_duplicates()  # no unique values in dask
    classes = [str(i) for i in classes]

    context.logger.info("Split and Train")
    X_train, X_test, y_train, y_test = model_selection.train_test_split(
        X, y, train_size=train_validation_size, random_state=random_state
    )

    scaler = StandardScaler()
    scaler = scaler.fit(X_train)
    X_train_transformed = scaler.transform(X_train)
    X_test_transformed = scaler.transform(X_test)

    model_config = gen_sklearn_model(model_pkg_class, context.parameters.items())

    model_config["FIT"].update({"X": X_train_transformed, "y": y_train})

    ClassifierClass = create_class(model_config["META"]["class"])

    model = ClassifierClass(**model_config["CLASS"])

    # load and fit model
    with joblib.parallel_backend("dask"):

        # initialize classifier from sklearn
        model = model.fit(**model_config["FIT"])

    # create reports
    context.logger.info("Evaluate")
    extra_data_dict = {}
    for report in (ROCAUC, ClassificationReport, ConfusionMatrix):

        report_name = str(report.__name__)
        # clear output
        plt.cla()
        plt.clf()
        plt.close()

        # genrate report
        viz = report(model, classes=classes, per_class=True, is_fitted=True)
        viz.fit(X_train_transformed, y_train)  # Fit the training data to the visualizer
        viz.score(
            X_test_transformed, y_test.compute()
        )  # Evaluate the model on the test data

        # log reports
        plot = context.log_artifact(
            PlotArtifact(report_name, body=viz.fig, title=report_name), db_key=False
        )
        extra_data_dict[str(report)] = plot

        # log results
        if report_name == "ROCAUC":
            context.log_results(
                {"micro": viz.roc_auc.get("micro"), "macro": viz.roc_auc.get("macro")}
            )

        elif report_name == "ClassificationReport":
            for score_name in viz.scores_:
                for score_class in viz.scores_[score_name]:

                    context.log_results(
                        {
                            score_name
                            + "-"
                            + score_class: viz.scores_[score_name].get(score_class)
                        }
                    )

        # viz.show()

    # get feature importance
    viz = FeatureImportances(
        model,
        classes=classes,
        per_class=True,
        is_fitted=True,
        labels=df_header.delete(df_header.get_loc(label_column)),
    )
    viz.fit(X_train_transformed, y_train)
    viz.score(X_test_transformed, y_test)
    # viz.show()

    plot = context.log_artifact(
        PlotArtifact("FeatureImportances", body=viz.fig, title="FeatureImportances"),
        db_key=False,
    )
    extra_data_dict[str("FeatureImportances")] = plot

    # clear final output
    plt.cla()
    plt.clf()
    plt.close()

    # log artifacts
    context.logger.info("Log artifacts")
    artifact_path = context.artifact_subpath(models_dest)

    # set label
    context.set_label("class", model_pkg_class)

    # log models
    context.log_model(
        "model",
        body=dumps(model),
        artifact_path=artifact_path,
        model_file="model.pkl",
        extra_data=extra_data_dict,
        metrics=context.results,
        labels={"class": model_pkg_class},
    )

    # log scalers
    context.log_artifact(
        "standard_scaler",
        body=dumps(scaler),
        artifact_path=artifact_path,
        model_file="scaler.gz",
        label="standard_scaler",
    )

    # log encoder
    context.log_artifact(
        "label_encoder",
        body=dumps(encoder),
        artifact_path=artifact_path,
        model_file="encoder.gz",
        label="label_encoder",
    )

    # set aside some test data
    df_to_save = delayed(np.column_stack)((X_test, y_test)).compute()
    context.log_dataset(
        test_set_key,
        df=pd.DataFrame(df_to_save, columns=df_header),  # improve log dataset ability
        format=file_ext,
        index=False,
        labels={"data-type": "held-out"},
        artifact_path=context.artifact_subpath("data"),
    )

    context.logger.info("Done!")
