"""Explicit algorithm registry; old artifacts never fall into a newer lane."""

from importlib import import_module

from StrategyV4 import ENGINE, MULTI_ENGINE, V42_ENGINE, VERSION, MULTI_VERSION, V42_VERSION

REGISTRY = {
    ENGINE: (VERSION, "StrategyV4", "candidate-v1.json", "evaluation.json", "replay-checkpoint.json"),
    MULTI_ENGINE: (
        MULTI_VERSION,
        "StrategyV41",
        "candidate-v2.json",
        "evaluation-v2.json",
        "replay-checkpoint-v2.json",
    ),
    V42_ENGINE: (V42_VERSION, "StrategyV42", "candidate-v3.json", "evaluation-v3.json", "replay-checkpoint-v3.json"),
}


def engine_module(engine):
    if engine not in REGISTRY:
        raise ValueError("未知自适应策略引擎")
    return import_module(REGISTRY[engine][1])


def algorithm_engine(algorithm):
    for engine, entry in REGISTRY.items():
        if entry[0] == algorithm:
            return engine
    raise ValueError("未知模型算法版本")


def template_for(engine, policy):
    module = engine_module(engine)
    return (module.adaptive_template if engine == ENGINE else module.template)(policy)
