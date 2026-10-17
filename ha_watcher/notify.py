"""Phase 4 fallback: Home Assistant persistent notifications for devices with no matching profile.

A notification has a stable id per device (`ha_watcher_unmatched_<device_id>`), so repeats update the
same notification instead of stacking. Notifications are not destructive, so they are sent regardless of
the provisioning dry-run/apply mode. Nothing here raises into the watcher (see `safe_call`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

diag = logging.getLogger("ha_watcher")

Caller = Callable[[dict], Awaitable[dict]]
ID_PREFIX = "ha_watcher_unmatched_"
MODES = ("off", "persistent")


def notification_id(device_id: str) -> str:
    return ID_PREFIX + device_id


def _pairs(items) -> str:
    out = []
    for it in items or []:
        out.append(":".join(str(x) for x in it) if isinstance(it, (list, tuple)) else str(it))
    return ", ".join(out) or "-"


def draft_command(device_id: str, profiles_dir: str | None) -> str:
    return f"python -m ha_watcher.draft_profile --device-id {device_id} --live --profiles-dir {profiles_dir or 'profiles'}"


def replay_command(device_id: str, profiles_dir: str | None) -> str:
    return f"python -m ha_watcher.provision --device-id {device_id} --profiles-dir {profiles_dir or 'profiles'}"


def build_notification(device_id: str, device: dict | None, entity_ids: list[str] | None = None,
                       reason: str | None = None, profiles_dir: str | None = "profiles") -> dict:
    """service_data for persistent_notification.create. `device` is a device-registry entry (or None)."""
    d = device or {}
    name = d.get("name_by_user") or d.get("name") or device_id
    mfr, model = d.get("manufacturer"), d.get("model")
    title = f"New device needs a profile: {name}" + (f" ({mfr} {model})" if mfr or model else "")
    lines = [
        "A device was added to Home Assistant that matches no active profile.",
        "",
        f"- **Name:** {name}",
        f"- **Device ID:** `{device_id}`",
        f"- **Manufacturer:** {mfr or '-'}",
        f"- **Model:** {model or '-'}",
        f"- **Identifiers:** {_pairs(d.get('identifiers'))}",
        f"- **Connections:** {_pairs(d.get('connections'))}",
        f"- **Suggested area:** {d.get('suggested_area') or '-'}",
    ]
    if entity_ids:
        lines.append(f"- **Entities:** {', '.join(entity_ids)}")
    if reason == "no_device_details":
        lines += ["", "Device details could not be read from the registry, so no match was possible."]
    lines += [
        "",
        "Generate a draft profile (run in the ha-watcher app directory with HA_URL and HA_TOKEN set):",
        "",
        f"    {draft_command(device_id, profiles_dir)}",
        "",
        "Review it, set `status: active`, then replay it against this device:",
        "",
        f"    {replay_command(device_id, profiles_dir)}            (dry-run; add --apply to execute)",
    ]
    return {"title": title, "message": "\n".join(lines), "notification_id": notification_id(device_id)}


def create_payload(service_data: dict) -> dict:
    return {"type": "call_service", "domain": "persistent_notification", "service": "create",
            "service_data": service_data}


def dismiss_payload(device_id: str) -> dict:
    return {"type": "call_service", "domain": "persistent_notification", "service": "dismiss",
            "service_data": {"notification_id": notification_id(device_id)}}


async def safe_call(caller: Caller, payload: dict, what: str) -> dict:
    """Send a notification command. Returns {"ok": bool, "error": ...}; never raises
    (except asyncio.CancelledError)."""
    try:
        msg = await caller(payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        diag.warning("notification %s failed: %r", what, exc)
        return {"ok": False, "error": repr(exc)}
    if not msg.get("success"):
        diag.warning("notification %s failed: %s", what, msg.get("error"))
        return {"ok": False, "error": msg.get("error")}
    return {"ok": True, "error": None}


async def notify_unmatched(caller: Caller, device_id: str, device: dict | None,
                           entity_ids: list[str] | None = None, reason: str | None = None,
                           profiles_dir: str | None = "profiles") -> dict:
    data = build_notification(device_id, device, entity_ids, reason, profiles_dir)
    res = await safe_call(caller, create_payload(data), f"create {data['notification_id']}")
    if res["ok"]:
        diag.info("notified unmatched device %s (%s)", device_id, data["notification_id"])
    return res


async def dismiss(caller: Caller, device_id: str) -> dict:
    res = await safe_call(caller, dismiss_payload(device_id), f"dismiss {notification_id(device_id)}")
    if res["ok"]:
        diag.info("dismissed unmatched notification for %s", device_id)
    return res
