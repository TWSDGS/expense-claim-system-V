
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from cache_utils import (
    append_sync_audit,
    load_master_snapshot,
    save_master_snapshot,
    load_pending_sync_queue,
    save_pending_sync_queue,
)


def _to_dt(v: Any) -> datetime:
    s = str(v or '').strip()
    if not s:
        return datetime.min
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return datetime.min


def _normalize_rows(rows: Any) -> List[Dict[str, Any]]:
    if isinstance(rows, pd.DataFrame):
        rows = rows.fillna('').to_dict(orient='records')
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
    return out


def _infer_system(item: Dict[str, Any]) -> str:
    payload = dict((item or {}).get('payload') or {})
    system_type = str(payload.get('system_type') or '').strip().lower()
    if system_type:
        return system_type
    op = str((item or {}).get('operation') or '').strip().lower()
    return 'travel' if 'travel' in op else 'expense'


def _normalize_operation(operation: str) -> str:
    op = str(operation or '').strip().lower()
    if 'hard' in op and 'delete' in op:
        return 'hard_delete'
    if 'soft' in op and 'delete' in op:
        return 'soft_delete'
    if 'restore' in op:
        return 'restore'
    if 'submit' in op:
        return 'submit'
    if 'draft' in op or 'save' in op:
        return 'save_draft'
    if 'delete' in op:
        return 'soft_delete'
    return op or 'save_draft'


def _choose_newer(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    old_v = int(old.get('version') or 0) if str(old.get('version') or '').strip().isdigit() else 0
    new_v = int(new.get('version') or 0) if str(new.get('version') or '').strip().isdigit() else 0
    if new_v != old_v:
        return dict(new if new_v > old_v else old)
    return dict(new if _to_dt(new.get('updated_at')) >= _to_dt(old.get('updated_at')) else old)


def merge_rows(base_rows: List[Dict[str, Any]], overlay_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for row in _normalize_rows(base_rows):
        rid = str(row.get('record_id') or '').strip()
        if rid:
            mapping[rid] = dict(row)
        else:
            extras.append(dict(row))
    for row in _normalize_rows(overlay_rows):
        rid = str(row.get('record_id') or '').strip()
        if not rid:
            extras.append(dict(row))
            continue
        if rid in mapping:
            mapping[rid] = _choose_newer(mapping[rid], row)
        else:
            mapping[rid] = dict(row)
    rows = list(mapping.values()) + extras
    rows.sort(key=lambda r: str(r.get('updated_at') or r.get('created_at') or ''), reverse=True)
    return rows


def overlay_pending_rows(base_rows: List[Dict[str, Any]], pending_items: List[Dict[str, Any]], system_type: str) -> List[Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for row in _normalize_rows(base_rows):
        rid = str(row.get('record_id') or '').strip()
        if rid:
            mapping[rid] = dict(row)
        else:
            extras.append(dict(row))
    items = [item for item in _normalize_rows(pending_items) if _infer_system(item) == system_type]
    items.sort(key=lambda item: str(item.get('queued_at') or ''))
    for item in items:
        payload = dict(item.get('payload') or {})
        rid = str(payload.get('record_id') or '').strip()
        op = _normalize_operation(item.get('operation') or '')
        existing = dict(mapping.get(rid, {})) if rid else {}
        if op == 'hard_delete':
            if rid in mapping:
                mapping.pop(rid, None)
            continue
        row = dict(existing)
        row.update(payload)
        row['system_type'] = system_type
        row['needs_sync'] = True
        row['sync_status'] = str(payload.get('sync_status') or 'pending')
        row['sync_message'] = str(payload.get('sync_message') or item.get('last_error') or '')
        if op == 'save_draft':
            row['status'] = str(payload.get('status') or 'draft')
        elif op == 'submit':
            row['status'] = 'submitted'
        elif op == 'restore':
            row['status'] = str(payload.get('status') or row.get('status') or 'draft')
        elif op == 'soft_delete':
            target = str(payload.get('status') or '').strip().lower()
            if not target:
                target = 'void' if str(existing.get('status') or '').strip().lower() in {'submitted', 'void'} else 'deleted'
            row['status'] = target
        if rid:
            mapping[rid] = row
        else:
            extras.append(row)
    rows = list(mapping.values()) + extras
    rows.sort(key=lambda r: str(r.get('updated_at') or r.get('created_at') or ''), reverse=True)
    return rows


def build_master_dataframe(
    system_type: str,
    actor_email: str,
    fetch_cloud_rows: Callable[[], Any],
    local_rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    system_type = str(system_type or '').strip().lower()
    actor_email = str(actor_email or '').strip().lower()
    cloud_source = 'cloud'
    cloud_rows: List[Dict[str, Any]] = []
    cloud_ok = False
    try:
        cloud_rows = _normalize_rows(fetch_cloud_rows())
        save_master_snapshot(system_type, actor_email, cloud_rows)
        cloud_ok = True
    except Exception as exc:
        cloud_rows = load_master_snapshot(system_type, actor_email)
        cloud_source = 'snapshot' if cloud_rows else 'empty'
        append_sync_audit({
            'event_type': 'cloud_fetch_failed',
            'system_type': system_type,
            'queue_owner_email': actor_email,
            'message': str(exc),
        })
    merged_rows = merge_rows(cloud_rows, _normalize_rows(local_rows or [])) if local_rows else list(cloud_rows)
    pending_items = load_pending_sync_queue(actor_email)
    master_rows = overlay_pending_rows(merged_rows, pending_items, system_type)
    df = pd.DataFrame(master_rows).fillna('') if master_rows else pd.DataFrame()
    if not df.empty and 'record_id' in df.columns:
        df = df.drop_duplicates(subset=['record_id'], keep='last')
    if not df.empty and 'owner_name' not in df.columns:
        if system_type == 'travel':
            df['owner_name'] = df.get('traveler', '')
        else:
            df['owner_name'] = df.get('employee_name', '')
    report = {
        'system_type': system_type,
        'cloud_source': cloud_source,
        'cloud_online': cloud_ok,
        'cloud_count': len(cloud_rows),
        'local_count': len(_normalize_rows(local_rows or [])),
        'pending_count': len([x for x in pending_items if _infer_system(x) == system_type]),
        'master_count': int(len(df.index)) if isinstance(df, pd.DataFrame) else 0,
        'last_checked_at': datetime.now().isoformat(timespec='seconds'),
    }
    return df, report


def sync_pending_events(system_type: str, actor: Any, api: Any) -> Dict[str, Any]:
    owner_email = str(getattr(actor, 'email', '') or '').strip().lower()
    queue = load_pending_sync_queue(owner_email)
    if not queue:
        return {'synced': 0, 'failed': 0, 'remaining': 0}
    new_queue: List[Dict[str, Any]] = []
    synced = 0
    failed = 0
    for item in queue:
        if _infer_system(item) != system_type:
            new_queue.append(item)
            continue
        payload = dict(item.get('payload') or {})
        rid = str(payload.get('record_id') or '').strip()
        op = _normalize_operation(item.get('operation') or '')
        try:
            if op == 'hard_delete':
                api.record_hard_delete(actor=actor, record_id=rid)
            elif op == 'soft_delete':
                api.record_soft_delete(actor=actor, record_id=rid)
            elif op == 'restore' and hasattr(api, 'record_restore'):
                api.record_restore(actor=actor, payload=payload)
            elif op == 'submit' or (op == 'restore' and str(payload.get('status') or '').strip().lower() == 'submitted'):
                api.record_submit(actor=actor, payload=payload)
            else:
                api.record_save_draft(actor=actor, payload=payload)
            synced += 1
            append_sync_audit({
                'event_type': 'sync_applied',
                'operation': op,
                'record_id': rid,
                'system_type': system_type,
                'queue_owner_email': owner_email,
                'event_id': item.get('event_id', ''),
            })
        except Exception as exc:
            failed += 1
            payload['needs_sync'] = True
            payload['sync_status'] = 'failed'
            payload['sync_message'] = str(exc)
            item['payload'] = payload
            item['retry_count'] = int(item.get('retry_count') or 0) + 1
            item['last_error'] = str(exc)
            new_queue.append(item)
            append_sync_audit({
                'event_type': 'sync_apply_failed',
                'operation': op,
                'record_id': rid,
                'system_type': system_type,
                'queue_owner_email': owner_email,
                'event_id': item.get('event_id', ''),
                'message': str(exc),
            })
    save_pending_sync_queue(new_queue, owner_email)
    return {'synced': synced, 'failed': failed, 'remaining': len([x for x in new_queue if _infer_system(x) == system_type])}
