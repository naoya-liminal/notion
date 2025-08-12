#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_notion_schema.py

JSON(domains.json / databases.json) をもとに、Notion Databases を作成・更新(差分適用)します。

主な改善点:
- resolve_or_create_database を実装（削除済みDBの再作成、ID自動更新フローに対応）
- Status の options/groups を API 送信前に除去（Notion API が拒否するため）
- Relation の single/dual_property を自動補完 + $ref を厳格解決（空UUID送信を防止）
- notion_search_db_by_title_and_parent の二重定義/挙動を統一（複数一致も検知）
- 変数定義順やグローバル定数(_UUID_RE)を整理（NameErrorを排除）
- 作成時の Title 列自動付与 ensure_title_prop_present
- --update-only, --write-back-ids, --ids-out の連携強化
- 余計な Status オプション更新/削除処理を削除（400回避）
- Support mapping-style "databases" (object) in databases.json; backward compatible with list.
"""

import argparse
import json
import os
import sys
import re
import time
from datetime import datetime, timezone
import shutil
from copy import deepcopy

import requests

API_BASE = "https://api.notion.com/v1"

# ----------------------------
# utils
# ----------------------------
def die(msg, code=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def headers(notion_version, token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": notion_version,
        "Content-Type": "application/json; charset=utf-8",
    }

def rt_to_plain(rt_array):
    if not isinstance(rt_array, list):
        return ""
    buf = []
    for r in rt_array:
        if r.get("type") == "text":
            t = r.get("text", {}).get("content", "")
        else:
            t = r.get(r.get("type", ""), {}).get("plain_text", "") or r.get("plain_text", "")
        if t:
            buf.append(t)
    return "".join(buf)

def http(session, method, url, **kwargs):
    for attempt in range(5):
        r = session.request(method, url, timeout=30, **kwargs)
        if r.status_code in (429, 502, 503):
            time.sleep(1 + attempt)
            continue
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"HTTP {r.status_code}: {detail}")
        return r
    raise RuntimeError("Max retries exceeded")

def canonicalize_uuid(s: str) -> str:
    if not isinstance(s, str):
        return s
    hexonly = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(hexonly) != 32:
        return s
    return "-".join([hexonly[0:8], hexonly[8:12], hexonly[12:16], hexonly[16:20], hexonly[20:32]]).lower()

def ensure_title_prop_present(props: dict) -> dict:
    """プロパティ内に title 列が無ければ '名称' を追加（作成時保険）。"""
    for _, schema in props.items():
        if "title" in schema:
            return props
    name = "名称"
    i = 1
    while name in props:
        name = f"名称_{i}"
        i += 1
    props[name] = {"title": {}}
    return props

def scrub_status_options(props: dict) -> dict:
    """
    Notion API は status.options / status.groups を受け付けないためリクエスト前に除去。
    """
    for _, schema in list(props.items()):
        if "status" in schema:
            schema["status"] = {}
    return props

def normalize_relation_kind(props: dict) -> dict:
    """Relation に single_property/dual_property のいずれかが無い場合は single_property を補完。"""
    for _, schema in list(props.items()):
        if "relation" in schema:
            rel = schema["relation"]
            if "single_property" not in rel and "dual_property" not in rel:
                rel["single_property"] = {}
    return props

# ----------------------------
# Notion HTTP wrappers
# ----------------------------
def notion_search_db_by_title_and_parent(session, notion_version, token, title_plain, parent_page_id):
    """
    Search API は曖昧検索のため、親ページID + タイトル完全一致で絞り込み、候補をすべて返す。
    """
    payload = {
        "query": title_plain[:100],
        "filter": {"value": "database", "property": "object"},
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        "page_size": 100,
    }
    r = http(session, "POST", f"{API_BASE}/search", headers=headers(notion_version, token), json=payload)
    data = r.json()
    matches = []
    for res in data.get("results", []):
        if res.get("object") != "database":
            continue
        parent = res.get("parent", {})
        if parent.get("type") == "page_id" and parent.get("page_id") == parent_page_id:
            if rt_to_plain(res.get("title", [])) == title_plain:
                matches.append(res)
    return matches

def notion_retrieve_database(session, notion_version, token, database_id):
    try:
        r = http(session, "GET", f"{API_BASE}/databases/{database_id}", headers=headers(notion_version, token))
        return r.json()
    except RuntimeError as e:
        msg = str(e)
        if "object_not_found" in msg or "Could not find database" in msg:
            raise RuntimeError(
                f"{msg}\nHINT: Share the DB & parent page with the integration, or the DB was deleted."
            )
        raise

def notion_create_database(session, notion_version, token, payload):
    r = http(session, "POST", f"{API_BASE}/databases", headers=headers(notion_version, token), json=payload)
    return r.json()

def notion_update_database(session, notion_version, token, database_id, payload):
    r = http(session, "PATCH", f"{API_BASE}/databases/{database_id}", headers=headers(notion_version, token), json=payload)
    return r.json()

# ----------------------------
# Schema planning
# ----------------------------
def split_properties_by_kind(props):
    """
    props を基に3分類:
    - base_props: (title, rich_text, number, select, multi_select, status, date, people, files, checkbox, url, email, phone_number, created_*, last_*)
    - relation_props
    - computed_props (formula, rollup)
    """
    base, rel, comp = {}, {}, {}
    for name, schema in props.items():
        if "relation" in schema:
            rel[name] = schema
        elif "formula" in schema or "rollup" in schema:
            comp[name] = schema
        else:
            base[name] = schema
    return base, rel, comp

def plan_property_additions(current_props, target_props):
    """
    現在のプロパティ(current_props)と目標(target_props)から、追加/軽微更新プランを返却。
    注意: Status の options 更新は API が非対応のため行わない。
    """
    to_add = {}
    to_update = {}
    for name, schema in target_props.items():
        if name not in current_props:
            to_add[name] = schema
        else:
            cur = current_props[name]
            # 軽微な更新のみ: number.format / select/multi-select の options 追加
            if "number" in schema and "number" in cur:
                tgt_fmt = schema["number"].get("format")
                cur_fmt = cur["number"].get("format")
                if tgt_fmt and tgt_fmt != cur_fmt:
                    to_update[name] = {"number": {"format": tgt_fmt}}
            elif "select" in schema and "select" in cur:
                cur_opts = {o["name"] for o in cur["select"].get("options", [])}
                add_opts = [o for o in schema["select"].get("options", []) if o["name"] not in cur_opts]
                if add_opts:
                    to_update[name] = {"select": {"options": list(cur["select"].get("options", [])) + add_opts}}
            elif "multi_select" in schema and "multi_select" in cur:
                cur_opts = {o["name"] for o in cur["multi_select"].get("options", [])}
                add_opts = [o for o in schema["multi_select"].get("options", []) if o["name"] not in cur_opts]
                if add_opts:
                    to_update[name] = {"multi_select": {"options": list(cur["multi_select"].get("options", [])) + add_opts}}
            # status の更新は送らない（追加/削除はUI運用）
    return to_add, to_update

def apply_updates(session, notion_version, token, dbid, to_add, to_update, dry_run):
    if not to_add and not to_update:
        return None
    update_payload = {"properties": {}}
    update_payload["properties"].update(to_add)
    update_payload["properties"].update(to_update)
    if dry_run:
        print(f"[DRY-RUN] PATCH /databases/{dbid} -> add:{list(to_add.keys())} update:{list(to_update.keys())}")
        return None
    return notion_update_database(session, notion_version, token, dbid, update_payload)

def apply_renames(session, notion_version, token, dbid, rename_map, current_props, dry_run):
    if not rename_map:
        return
    payload = {"properties": {}}
    for old, new in rename_map.items():
        if old in current_props and new not in current_props:
            payload["properties"][old] = {"name": new}
    if not payload["properties"]:
        return
    if dry_run:
        print(f"[DRY-RUN] RENAME in /databases/{dbid} -> {rename_map}")
        return
    notion_update_database(session, notion_version, token, dbid, payload)

# ----------------------------
# Relation helpers
# ----------------------------
def resolve_relation_ref(rel: dict, key_to_dbid: dict, ctx: str):
    """
    relation.database_id に "$ref:<key>" または <uuid> を許容。必ず妥当なUUIDに確定させる。
    """
    ref_key = None
    dbid = rel.get("database_id")
    if isinstance(dbid, str) and dbid.startswith("$ref:"):
        ref_key = dbid.split(":", 1)[1]
    else:
        ref_key = rel.get("$ref") or rel.get("database_key") or rel.get("ref")

    if ref_key:
        if ref_key not in key_to_dbid:
            raise RuntimeError(f"{ctx}: relation refers to unknown key '{ref_key}'. Known keys: {list(key_to_dbid.keys())}")
        rel["database_id"] = key_to_dbid[ref_key]
    else:
        if isinstance(dbid, str):
            dbid = canonicalize_uuid(dbid)
            dbid = dbid.strip()
            if dbid:
                rel["database_id"] = dbid
                return
        raise RuntimeError(f"{ctx}: relation.database_id must be a non-empty string or $ref:<key>; got '{rel.get('database_id')}'.")

def replace_refs_in_relations(databases_cfg, key_to_dbid):
    for db in databases_cfg:
        props = db["payload"].get("properties", {}) or {}
        for name, schema in list(props.items()):
            if "relation" in schema:
                resolve_relation_ref(schema["relation"], key_to_dbid, f"[{db['key']}].properties.{name}")

def validate_relations(databases_cfg):
    bad = []
    for db in databases_cfg:
        for name, schema in (db["payload"].get("properties", {}) or {}).items():
            if "relation" in schema:
                rel = schema["relation"]
                dbid = rel.get("database_id")
                has_kind = ("single_property" in rel) or ("dual_property" in rel)
                if dbid is None or (isinstance(dbid, str) and not dbid.strip()):
                    bad.append(f"[{db['key']}].{name}.database_id is empty")
                if not has_kind:
                    bad.append(f"[{db['key']}].{name} missing single/dual_property")
    if bad:
        raise RuntimeError("Invalid relation definitions:\n  " + "\n  ".join(bad))

# ----------------------------
# Config helpers
# ----------------------------
def normalize_databases_config(config_obj):
    """Normalize config.databases to a list of entries with injected 'key'.
    Returns (databases_list, source_kind) where source_kind is 'list' or 'dict'.
    """
    dbs = config_obj.get("databases")
    if isinstance(dbs, dict):
        lst = []
        for k, v in dbs.items():
            entry = {"key": k}
            for kk, vv in v.items():
                if kk == "key":
                    continue
                entry[kk] = vv
            lst.append(entry)
        return lst, "dict"
    elif isinstance(dbs, list):
        # ensure each item has a key
        lst = []
        for item in dbs:
            e = dict(item)
            if "key" not in e or not e["key"]:
                # derive from title text if possible
                title_plain = rt_to_plain(((e.get("payload") or {}).get("title") or [])) or "db"
                e["key"] = re.sub(r"\s+", "_", title_plain)
            lst.append(e)
        return lst, "list"
    else:
        die("config.databases must be a list or an object (mapping)")

def ensure_parent_on_payloads(databases_cfg, parent_page_id):
    for db in databases_cfg:
        p = db["payload"].get("parent") or {}
        if p.get("type") != "page_id" or not p.get("page_id"):
            db["payload"]["parent"] = {"type": "page_id", "page_id": parent_page_id}

def save_json_atomic(path, data):
    """Atomic-ish write: write to tmp, backup old, then replace."""
    dirpath = os.path.dirname(os.path.abspath(path)) or "."
    tmppath = os.path.join(dirpath, f".{os.path.basename(path)}.tmp")
    with open(tmppath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
    os.replace(tmppath, path)

def write_back_ids_to_databases_json(databases_json_path, config_obj, key_to_dbid, source_kind):
    """Persist resolved database_id(s) back into databases.json for both list/dict layouts."""
    updated = deepcopy(config_obj)
    changed = 0
    if source_kind == "list":
        for db in updated.get("databases", []):
            k = db.get("key")
            if not k:
                continue
            new_id = key_to_dbid.get(k)
            if new_id and db.get("database_id") != new_id:
                db["database_id"] = new_id
                changed += 1
    elif source_kind == "dict":
        for k, body in (updated.get("databases") or {}).items():
            new_id = key_to_dbid.get(k)
            if new_id and body.get("database_id") != new_id:
                body["database_id"] = new_id
                changed += 1
    else:
        die("unknown databases source_kind")

    if changed:
        save_json_atomic(databases_json_path, updated)
        print(f"[APPLY] Wrote {changed} database_id(s) back to {databases_json_path}")
    else:
        print("[SKIP] No database_id changes to persist.")

def write_ids_mapping(path, key_to_dbid):
    payload = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "ids": key_to_dbid}
    save_json_atomic(path, payload)
    print(f"[APPLY] Wrote key->database_id mapping to {path}")

# ----------------------------
# Resolver (create-on-missing)
# ----------------------------
def resolve_or_create_database(session, notion_version, token, db_entry, minimal_payload, args):
    """
    解決手順:
      1) database_id が有効なら retrieve
      2) (title + parent) で厳密検索
      3) 見つからなければ --update-only でエラー、それ以外は作成
    """
    key = db_entry["key"]
    parent_id = minimal_payload.get("parent", {}).get("page_id")
    title_plain = rt_to_plain(minimal_payload.get("title", [])) or "<NO_TITLE>"

    # 1) try by database_id
    dbid = db_entry.get("database_id")
    if dbid:
        dbid = canonicalize_uuid(dbid)
        try:
            resolved = notion_retrieve_database(session, notion_version, token, dbid)
            return resolved
        except RuntimeError as e:
            msg = str(e)
            if "object_not_found" in msg or "Could not find database" in msg:
                if args.update_only:
                    die(f"[{key}] database_id={dbid} は削除/不可視です (--update-only)。DBを復元するか database_id を削除してください。")
                print(f"[WARN] [{key}] database_id={dbid} は無効。タイトル+親で再解決し、必要なら作成します。", file=sys.stderr)
            else:
                raise

    # 2) try exact (title + parent)
    matches = notion_search_db_by_title_and_parent(session, notion_version, token, title_plain, parent_id)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        die(f"[{key}] 親 {parent_id} / タイトル '{title_plain}' に一致する DB が複数。'database_id' を設定してください。")

    # 3) create if allowed
    if args.update_only:
        die(f"[{key}] 親 {parent_id} / タイトル '{title_plain}' の DB が見つかりません (--update-only)。")

    if args.dry_run:
        print(f"[DRY-RUN] CREATE database '{title_plain}' under page {parent_id}")
        return {"id": f"DUMMY-{key}", "properties": {}, "title": minimal_payload.get("title", [])}

    # ensure title property at creation time
    minimal_payload["properties"] = ensure_title_prop_present(minimal_payload.get("properties", {}) or {})
    created = notion_create_database(session, notion_version, token, minimal_payload)
    return created

# ----------------------------
# main
# ----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", required=True, help="domains.json path")
    ap.add_argument("--databases", required=True, help="databases.json path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-removals", action="store_true")
    ap.add_argument(
        "--write-back-ids",
        action="store_true",
        help="Resolve/create 後に database_id を databases.json に書き戻す（in-place）。",
    )
    ap.add_argument("--ids-out", help="key->database_id のマッピングを別ファイルにも出力。")
    ap.add_argument("--update-only", action="store_true", help="Do not create databases; error if not found.")
    args = ap.parse_args()

    token = os.getenv("NOTION_TOKEN")
    if not token:
        die("環境変数 NOTION_TOKEN が未設定です。")

    domains = load_json(args.domains)  # 参照用途（現時点では利用しないが将来の検証・整形に使用予定）
    config = load_json(args.databases)

    notion_version = config.get("defaults", {}).get("notion_version") or "2022-06-28"
    parent_page_id = config.get("defaults", {}).get("parent_page_id")

    databases_cfg, databases_source_kind = normalize_databases_config(config)
    if not databases_cfg:
        die("databases が空です。")

    # 事前正規化（status除去 / relation補完）
    for db in databases_cfg:
        p = db["payload"].get("properties", {}) or {}
        p = scrub_status_options(p)
        p = normalize_relation_kind(p)
        db["payload"]["properties"] = p

    session = requests.Session()

    # 親が未設定なら defaults で補完
    if parent_page_id and parent_page_id != "REPLACE_WITH_PAGE_ID":
        ensure_parent_on_payloads(databases_cfg, parent_page_id)
    else:
        print("[WARN] defaults.parent_page_id が未設定/プレースホルダです。payload.parent をそのまま使用します。", file=sys.stderr)

    # --- Phase 0: 生成時は Relation/Formula/Rollup を除いた最小構成で作成（後で追加） ---
    base_payloads = {}
    for db in databases_cfg:
        full_props = db["payload"].get("properties", {}) or {}
        base, rel, comp = split_properties_by_kind(full_props)
        minimal = deepcopy(db["payload"])
        # create 時の保険（Title列付与）。既存DBの更新には影響しない。
        minimal["properties"] = ensure_title_prop_present(base if base else {})
        base_payloads[db["key"]] = {"base": base, "rel": rel, "comp": comp, "minimal_payload": minimal}

    # --- Phase 1: database_id の解決/作成 ---
    key_to_dbid = {}
    current_schema = {}

    for db in databases_cfg:
        key = db["key"]
        desired_payload = base_payloads[key]["minimal_payload"]
        resolved = resolve_or_create_database(session, notion_version, token, db, desired_payload, args)

        if not resolved or not resolved.get("id"):
            die(f"データベース '{key}' の解決/作成に失敗しました。")

        key_to_dbid[key] = resolved["id"]
        current_schema[key] = resolved

    # --- Phase 2: Relation の $ref 解決（in-memory） ---
    try:
        replace_refs_in_relations(databases_cfg, key_to_dbid)
        # base_payloads も更新 + relation 形状の再補正
        for db in databases_cfg:
            p = db["payload"]["properties"]
            p = normalize_relation_kind(p)
            db["payload"]["properties"] = p
            key = db["key"]
            full_props = p
            base, rel, comp = split_properties_by_kind(full_props)
            base_payloads[key]["base"], base_payloads[key]["rel"], base_payloads[key]["comp"] = base, rel, comp
        # 早期検証
        validate_relations(databases_cfg)
    except Exception as e:
        die(f"Relation の参照解決に失敗: {e}")

    # --- Persist resolved IDs (if requested) ---
    if args.write_back_ids:
        if args.dry_run:
            print("[INFO] --write-back-ids は --dry-run 中はスキップ（DUMMY ID を保存しないため）。")
        else:
            write_back_ids_to_databases_json(args.databases, config, key_to_dbid, databases_source_kind)

    if args.ids_out:
        write_ids_mapping(args.ids_out, key_to_dbid)

    # --- Phase 3: 差分適用 ---
    # 3-1: rename_map
    for db in databases_cfg:
        key = db["key"]
        dbid = key_to_dbid[key]
        rename_map = db.get("rename_map") or {}
        cur_props = (current_schema[key].get("properties") or {})
        apply_renames(session, notion_version, token, dbid, rename_map, cur_props, args.dry_run)
        if not args.dry_run:
            current_schema[key] = notion_retrieve_database(session, notion_version, token, dbid)

    # 3-2: Base properties 追加/更新
    for db in databases_cfg:
        key = db["key"]
        dbid = key_to_dbid[key]
        target_base = base_payloads[key]["base"]
        cur_props = current_schema[key].get("properties") or {}
        to_add, to_update = plan_property_additions(cur_props, target_base)
        apply_updates(session, notion_version, token, dbid, to_add, to_update, args.dry_run)
        if not args.dry_run and (to_add or to_update):
            current_schema[key] = notion_retrieve_database(session, notion_version, token, dbid)

    # 3-3: Relation 追加/更新
    for db in databases_cfg:
        key = db["key"]
        dbid = key_to_dbid[key]
        target_rel = base_payloads[key]["rel"]
        if not target_rel:
            continue
        cur_props = current_schema[key].get("properties") or {}
        to_add, to_update = plan_property_additions(cur_props, target_rel)
        apply_updates(session, notion_version, token, dbid, to_add, to_update, args.dry_run)
        if not args.dry_run and (to_add or to_update):
            current_schema[key] = notion_retrieve_database(session, notion_version, token, dbid)

    # 3-4: Formula/Rollup（依存関係の後に） — apply one-by-one for better diagnostics
    for db in databases_cfg:
        key = db["key"]
        dbid = key_to_dbid[key]
        target_comp = base_payloads[key]["comp"]
        if not target_comp:
            continue
        for pname, pschema in target_comp.items():
            cur_props = current_schema[key].get("properties") or {}
            need_add = pname not in cur_props
            need_update = (not need_add) and (pschema != cur_props.get(pname))
            if not (need_add or need_update):
                continue
            single_payload = {"properties": {pname: pschema}}
            if args.dry_run:
                print(f"[DRY-RUN] PATCH /databases/{dbid} -> computed {pname}")
                continue
            try:
                notion_update_database(session, notion_version, token, dbid, single_payload)
                current_schema[key] = notion_retrieve_database(session, notion_version, token, dbid)
            except Exception as e:
                print(
                    f"[WARN] Failed to apply computed property {key}.{pname}. "
                    f"Expression: {pschema.get('formula', {}).get('expression')}. Error: {e}",
                    file=sys.stderr,
                )
                raise

    # 3-5: 不要オプション/プロパティ削除（--allow-removals）
    if args.allow_removals and not args.dry_run:
        for db in databases_cfg:
            key = db["key"]
            dbid = key_to_dbid[key]
            cur_props = current_schema[key].get("properties") or {}
            target_all = db["payload"]["properties"]

            # プロパティ削除
            removals = [name for name in cur_props.keys() if name not in target_all.keys()]
            if removals:
                payload = {"properties": {name: None for name in removals}}
                print(f"[APPLY] REMOVE props in {key}: {removals}")
                notion_update_database(session, notion_version, token, dbid, payload)
                current_schema[key] = notion_retrieve_database(session, notion_version, token, dbid)

            # Select/Multi-select のオプション削除（Status は API 不可のためスキップ）
            for name, schema in target_all.items():
                if name not in cur_props:
                    continue
                if "select" in schema and "select" in cur_props[name]:
                    tgt = {o["name"] for o in schema["select"].get("options", [])}
                    cur = {o["name"] for o in cur_props[name]["select"].get("options", [])}
                    to_del = list(cur - tgt)
                    if to_del:
                        new_opts = [o for o in cur_props[name]["select"].get("options", []) if o["name"] in tgt]
                        notion_update_database(session, notion_version, token, dbid,
                                               {"properties": {name: {"select": {"options": new_opts}}}})
                if "multi_select" in schema and "multi_select" in cur_props[name]:
                    tgt = {o["name"] for o in schema["multi_select"].get("options", [])}
                    cur = {o["name"] for o in cur_props[name]["multi_select"].get("options", [])}
                    to_del = list(cur - tgt)
                    if to_del:
                        new_opts = [o for o in cur_props[name]["multi_select"].get("options", []) if o["name"] in tgt]
                        notion_update_database(session, notion_version, token, dbid,
                                               {"properties": {name: {"multi_select": {"options": new_opts}}}})
                # Status は UI 管理。変更は送信しない。

    if args.dry_run:
        print("\n[DRY-RUN] 計画の適用は行っていません。--dry-run を外して実行してください。")

    print("[OK] 完了")
    return 0

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        die(str(e), 2)