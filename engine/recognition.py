"""Evidence projection only: no UI input or guessed AutomationIds/business values."""
from collections import Counter
import json


def _usable(node):
    return isinstance(node, dict) and (any(node.get(key) is not None and node.get(key) != ""
        for key in ("role", "name", "value", "native_id", "ref_id", "ref"))
        or bool(node.get("children")))


def derive(raw, stage="window"):
    data = raw.get("data", {}) if isinstance(raw, dict) else {}
    root = data.get("tree") if isinstance(data, dict) else None
    if not isinstance(root, dict) or raw.get("ok") is not True or not _usable(root):
        return {"status": "empty" if isinstance(root, dict) and raw.get("ok") is True else "unavailable",
                "stage": stage, "node_count": 0, "nodes": [], "complete": False,
                "ground_truth": "not_supplied", "recognition_validated": False}
    nodes, pending = [], [(root, None, "0")]
    while pending:
        if len(nodes) >= 20000:
            break
        node, parent, path = pending.pop()
        if not _usable(node):
            continue
        children = node.get("children", [])
        if not isinstance(children, list):
            children = []
        role = str(node.get("role", "unknown"))
        states = node.get("states", [])
        selected = (True if isinstance(states, list) and "selected" in states else
                    states.get("selected") if isinstance(states, dict) else node.get("selected"))
        reported_children = node.get("children_count")
        if type(reported_children) is not int or reported_children < 0:
            reported_children = None
        nodes.append({"path": path, "parent_path": parent, "role": role,
                      "name": node.get("name"), "value": node.get("value"),
                      "native_id": node.get("native_id"), "ref": node.get("ref_id", node.get("ref")),
                      "selected": selected if type(selected) is bool else None,
                      "children_count": len(children), "reported_children_count": reported_children,
                      "states": states,
                      "bounds": node.get("bounds"), "patterns": node.get("patterns"),
                      "available_actions": node.get("available_actions", []),
                      "truncated": bool(node.get("truncated") or node.get("subtree_truncated"))})
        for i, child in reversed(list(enumerate(children))):
            if isinstance(child, dict):
                pending.append((child, path, path + "/" + str(i)))
    signatures = Counter(json.dumps([n["role"], n["native_id"], n["name"]],
                                    sort_keys=True, ensure_ascii=False) for n in nodes)
    roles = Counter(n["role"] for n in nodes)
    truncated = bool(pending) or bool(data.get("truncated")) or any(n["truncated"] for n in nodes)
    def evidence(node):
        return {"path": node["path"], "role": node["role"], "name_observed": node["name"],
                "value_observed": node["value"], "selected_observed": node["selected"],
                "native_id_observed": node["native_id"], "code_display_mapping": "unproven"}
    def family(roles):
        return [evidence(n) for n in nodes if n["role"] in roles]
    return {"status": "observed" if nodes else "empty", "stage": stage,
            "node_count": len(nodes), "nodes": nodes, "roles": dict(roles),
            "complete": False if truncated else data.get("complete"), "projection_truncated": bool(pending),
            "duplicate_signature_groups": sum(v > 1 for v in signatures.values()),
            "selected_nodes": sum(n["selected"] is True for n in nodes),
            "pattern_metadata_exposed": any(n["patterns"] is not None for n in nodes),
            "combo_candidates": [n["path"] for n in nodes if n["role"] in {"combobox", "listbox"}],
            "table_candidates": [n["path"] for n in nodes if n["role"] in {"table", "grid", "treegrid"}],
            "menu_candidates": [n["path"] for n in nodes if n["role"] in {"menu", "menuitem", "menubar"}],
            "combo_evidence": family({"combobox", "listbox", "option", "listitem"}),
            "table_evidence": family({"table", "grid", "treegrid", "row", "cell", "columnheader", "rowheader"}),
            "menu_evidence": family({"menu", "menuitem", "menubar", "menubutton"}),
            "menu_evidence_status": "observed" if any(n["role"] in {"menu", "menuitem", "menubar", "menubutton"}
                                                       for n in nodes) else "unavailable",
            "editor_evidence": family({"textbox", "text", "spinbutton", "combobox"}),
            "empty_container_candidates": [n["path"] for n in nodes
                if not n["children_count"] and not n["truncated"]
                and not (n["reported_children_count"] or 0)
                and n["role"] in {"tree", "window", "group", "unknown"}],
            "business_code_mapping": "unproven", "cell_addressability": "unproven",
            "empty_peer_cause": "unproven", "business_commit": "not_observed",
            "ground_truth": "not_supplied", "recognition_validated": False}
