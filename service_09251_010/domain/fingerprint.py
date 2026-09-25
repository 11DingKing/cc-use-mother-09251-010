"""输入指纹：对每次计算的输入做可验证的摘要。

指纹只覆盖“业务内容”（窗口、口径哈希、证据业务键与内容哈希），
不包含内部行号，因此同一组输入在任何时刻重算都得到同一指纹，
这是可复算口径与复核机制的根基。
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(obj: object) -> str:
    """确定性 JSON：键排序、紧凑分隔符、禁止 NaN。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(kind: str, source: str, external_id: str, occurred_at: str, payload: dict) -> str:
    """证据内容哈希：覆盖业务身份字段与负载，重放同一份证据必然得到同一哈希。"""
    return "sha256:" + sha256_hex(canonical_json({
        "kind": kind,
        "source": source,
        "external_id": external_id,
        "occurred_at": occurred_at,
        "payload": payload,
    }))


def manifest_fingerprint_view(manifest: list[dict]) -> list[dict]:
    """清单的指纹视图：剔除内部标识，只保留业务键与内容哈希。"""
    return [
        {
            "kind": entry["kind"],
            "source": entry["source"],
            "external_id": entry["external_id"],
            "occurred_at": entry["occurred_at"],
            "content_hash": entry["content_hash"],
        }
        for entry in manifest
    ]


def input_fingerprint(
    window_spec: dict,
    baseline_spec: dict | None,
    metric_defs: list[dict],
    manifest: list[dict],
) -> str:
    """一次计算的输入指纹：窗口 + 口径版本 + 证据清单。"""
    return "sha256:" + sha256_hex(canonical_json({
        "window": window_spec,
        "baseline": baseline_spec,
        "metrics": sorted(
            (
                {
                    "name": entry["definition"]["name"],
                    "version": entry["definition"]["version"],
                    "definition_hash": entry["definition_hash"],
                }
                for entry in metric_defs
            ),
            key=lambda item: (item["name"], item["version"]),
        ),
        "evidence": manifest_fingerprint_view(manifest),
    }))


def step_fingerprint(
    metric_def_entry: dict,
    window_spec: dict,
    baseline_spec: dict | None,
    manifest: list[dict],
) -> str:
    """单个指标步骤的指纹：只纳入该口径实际读取的证据类型。"""
    source_kind = metric_def_entry["definition"]["source_kind"]
    relevant = [entry for entry in manifest if entry["kind"] == source_kind]
    return "sha256:" + sha256_hex(canonical_json({
        "definition_hash": metric_def_entry["definition_hash"],
        "window": window_spec,
        "baseline": baseline_spec,
        "evidence": manifest_fingerprint_view(relevant),
    }))


def manifest_hash(manifest: list[dict]) -> str:
    """清单整体哈希，用于导出文件快速核对输入集合。"""
    return "sha256:" + sha256_hex(canonical_json(manifest_fingerprint_view(manifest)))
