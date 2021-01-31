import logging
from typing import Any, Dict, Sequence, Union

import numpy as np
from sacred import Experiment
import seml
import torch

from rgnn_at_scale.data import prep_graph, split
from rgnn_at_scale.attacks import create_attack, SPARSE_ATTACKS
from rgnn_at_scale.io import Storage
from rgnn_at_scale.models import DenseGCN, GCN
from rgnn_at_scale.train import train
from rgnn_at_scale.utils import accuracy


ex = Experiment()
seml.setup_logger(ex)


@ex.post_run_hook
def collect_stats(_run):
    seml.collect_exp_stats(_run)


@ex.config
def config():
    overwrite = None
    db_collection = None
    if db_collection is not None:
        ex.observers.append(seml.create_mongodb_observer(db_collection, overwrite=overwrite))

    # default params
    dataset = 'cora_ml'  # Options are 'cora_ml' and 'citeseer' (or with a big GPU 'pubmed')
    attack = 'LocalPRBCD'
    attack_params = {}
    nodes = [0, 1, 2]
    epsilons = [10] #0.1, 0.25, 0.5, 1]
    binary_attr = False
    seed = 0
    artifact_dir = 'cache_debug'
    model_storage_type = 'pretrained'
    device = 0
    display_steps = 10
    model_label = 'Vanilla PPRGo'
    make_undirected = True
    make_unweighted = True
    data_dir = './datasets'
    data_device = 'cpu'


@ex.automain
def run(data_dir: str, dataset: str, attack: str, attack_params: Dict[str, Any], nodes: str, epsilons: Sequence[float],
        binary_attr: bool, make_undirected: bool, make_unweighted: bool, seed: int,
        artifact_dir: str, model_label: str, model_storage_type: str, device: Union[str, int],
        data_device: Union[str, int], display_steps: int):
    logging.info({
        'dataset': dataset, 'attack': attack, 'attack_params': attack_params, 'nodes': nodes, 'epsilons': epsilons,
        'binary_attr': binary_attr, 'seed': seed,
        'artifact_dir': artifact_dir, 'model_label': model_label, 'model_storage_type': model_storage_type,
        'device': device, 'display_steps': display_steps
    })

    assert sorted(epsilons) == epsilons, 'argument `epsilons` must be a sorted list'
    assert len(np.unique(epsilons)) == len(epsilons),\
        'argument `epsilons` must be unique (strictly increasing)'
    assert all([eps >= 0 for eps in epsilons]), 'all elements in `epsilons` must be greater than 0'

    results = []
    graph = prep_graph(dataset, data_device, dataset_root=data_dir,
                       make_undirected=make_undirected,
                       make_unweighted=make_unweighted,
                       binary_attr=binary_attr,
                       return_original_split=dataset.startswith('ogbn'))
    attr, adj, labels = graph[:3]
    if len(graph) == 3:
        idx_train, idx_val, idx_test = split(labels.cpu().numpy())
    else:
        idx_train, idx_val, idx_test = graph[3]['train'], graph[3]['valid'], graph[3]['test']
    n_features = attr.shape[1]
    n_classes = int(labels.max() + 1)

    storage = Storage(artifact_dir, experiment=ex)

    model_params = dict(dataset=dataset,
                        binary_attr=binary_attr,
                        seed=seed)

    # if epsilons[0] != 0:
    #     epsilons = list(epsilons)
    #     epsilons.insert(0, 0)

    if model_label is not None and model_label:
        model_params['label'] = model_label
    models_and_hyperparams = storage.find_models(model_storage_type, model_params)

    for model, hyperparams in models_and_hyperparams:
        model = model.to(device)
        model.eval()

        adversary = create_attack(attack, binary_attr, attr, adj=adj, labels=labels,
                                  model=model, idx_attack=idx_test, device=device, **attack_params)

        for node in nodes:
            degree = adj[node].sum()
            for eps in epsilons:
                n_perturbations = int((eps * degree).round().item())
                if n_perturbations == 0:
                    continue

                # In case the model is non-deterministic to get the results either after attacking or after loading
                torch.manual_seed(seed)
                np.random.seed(seed)

                logits = adversary.attack(node, n_perturbations)

                results.append({
                    'label': hyperparams['label'],
                    'epsilon': eps,
                    'n_perturbations': n_perturbations,
                    'degree': int(degree.item()),
                    'logits': logits,
                    'node_id': node,
                })

    return {
        'results': results
    }
