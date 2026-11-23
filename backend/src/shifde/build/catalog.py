"""The executor catalog: which executors exist and how mature each is.

Maturity is derived, never stored (P3): `builtin.function` ships with the platform and is always
`validated`; the connection-backed executors inherit the derived maturity of the adapter capability
they depend on (declared adapter + recorded conformance evidence).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from shifde.adapters import evidence
from shifde.adapters.maturity import capability_maturity
from shifde.adapters.registry import AdapterRegistry
from shifde.build import schemas as s
from shifde.build.executors import ADAPTER_OF, BUILTIN_FUNCTIONS, EXECUTORS, binding_schema


def maturity_map(session: Session, registry: AdapterRegistry) -> dict[str, str]:
    latest = evidence.latest_runs(session)
    out: dict[str, str] = {"builtin.function": "validated"}
    for executor, (adapter_key, capability) in ADAPTER_OF.items():
        out[executor] = _derived(registry, latest, adapter_key, capability)
    return out


def adapter_maturity(
    session: Session, registry: AdapterRegistry, adapter_key: str, capability: str
) -> str:
    """The derived maturity of one adapter capability (`planned` for an unknown adapter)."""
    return _derived(registry, evidence.latest_runs(session), adapter_key, capability)


def _derived(
    registry: AdapterRegistry,
    latest: dict[str, evidence.ConformanceRun],
    adapter_key: str,
    capability: str,
) -> str:
    descriptor = registry.descriptor(adapter_key)
    if descriptor is None:
        return "planned"
    return capability_maturity(descriptor, capability, latest.get(adapter_key)).maturity


def executor_catalog(session: Session, registry: AdapterRegistry) -> s.ExecutorCatalogOut:
    maturity = maturity_map(session, registry)
    items: list[s.ExecutorOut] = []
    for key in EXECUTORS:
        functions = None
        if key == "builtin.function":
            functions = [
                s.FunctionOut(
                    key=f.key,
                    description=f.description,
                    input_schema=f.input_schema,
                    output_schema=f.output_schema,
                    side_effect_class=f.side_effect_class,
                )
                for f in BUILTIN_FUNCTIONS.values()
            ]
        items.append(
            s.ExecutorOut(
                key=key,
                maturity=maturity[key],
                binding_schema=binding_schema(key),
                functions=functions,
            )
        )
    return s.ExecutorCatalogOut(items=items)


def node_type_catalog(session: Session, registry: AdapterRegistry) -> s.NodeTypeCatalogOut:
    """The pipeline node-type registry (P6 section 2). A type is `enabled` only when its executor
    exists and the adapter capability it depends on is not `planned`; the maturity of an
    adapter-backed type is the derived maturity of that capability, every other implemented type
    is `validated` by the executor tests shipped with the runtime. Never stored."""
    from shifde.build import pipeline_nodes as nodes

    maturity = maturity_map(session, registry)
    items: list[s.NodeTypeOut] = []
    ordered = sorted(
        nodes.all_types(),
        key=lambda t: (
            nodes.NODE_TYPE_ORDER.index(t.type)
            if t.type in nodes.NODE_TYPE_ORDER
            else len(nodes.NODE_TYPE_ORDER)
        ),
    )
    for nt in ordered:
        level = maturity.get(nt.executor, "planned") if nt.executor else "validated"
        enabled = nt.implemented and level != "planned"
        reason = None
        if not enabled:
            reason = nt.reason_disabled or (
                "its executor is not implemented"
                if not nt.implemented
                else "the adapter it depends on is planned"
            )
        items.append(
            s.NodeTypeOut(
                type=nt.type,
                label=nt.label,
                category=nt.category,
                description=nt.description,
                config_schema=nt.config_schema,
                inputs=[port_out(p) for p in nt.inputs],
                outputs=[port_out(p) for p in nt.outputs],
                dynamic_inputs=nt.dynamic_inputs,
                side_effect=nt.side_effect,
                enabled=enabled,
                maturity=level if enabled else "planned",
                reason_disabled=reason,
            )
        )
    return s.NodeTypeCatalogOut(items=items)


def port_out(port: object) -> s.PortOut:
    from shifde.build.pipeline_nodes import Port

    assert isinstance(port, Port)
    return s.PortOut.model_validate(
        {"name": port.name, "schema": port.schema, "required": port.required}
    )
