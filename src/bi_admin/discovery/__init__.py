"""Model discovery — introspected registry of models, fields and the relation graph."""

from bi_admin.discovery.registry import (
    FieldInfo,
    ModelInfo,
    PathStep,
    Registry,
    RelationInfo,
    build_model_info,
    discover,
    get_registry,
    model_label,
    reset_registry,
)

__all__ = [
    "FieldInfo",
    "ModelInfo",
    "PathStep",
    "Registry",
    "RelationInfo",
    "build_model_info",
    "discover",
    "get_registry",
    "model_label",
    "reset_registry",
]
