"""Wave 4B: doctor checks backing the production Docker container story.

Covers the two checks added in ``web_agent/doctor.py``:

* ``_check_not_running_as_root`` -- warns at euid==0, ok when non-root,
  and skips cleanly on platforms without ``os.geteuid`` (Windows).
* ``_check_container_sandbox`` -- informational container detection +
  Chromium-sandbox state reporting.

Plus the shared ``_detect_container`` helper and registration in
``run_doctor``. All offline; no browser launch, no container required
(detection is monkeypatched / driven via env + filesystem stand-ins).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from web_agent import doctor as doctor_mod
from web_agent.config import AppConfig
from web_agent.doctor import (
    _check_container_sandbox,
    _check_not_running_as_root,
    _detect_container,
    run_doctor,
)

# ----------------------------------------------------------------------
# _check_not_running_as_root
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_check_warns_when_euid_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    check = await _check_not_running_as_root(AppConfig())
    assert check.name == "not_running_as_root"
    assert check.status == "warn"
    assert "root" in check.message.lower()


@pytest.mark.asyncio
async def test_root_check_ok_when_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    check = await _check_not_running_as_root(AppConfig())
    assert check.status == "ok"
    assert "1000" in check.message


@pytest.mark.asyncio
async def test_root_check_skips_without_geteuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows / any platform lacking os.geteuid -> skip, never crash.

    We delete the attribute if present so the probe takes the
    ``getattr(os, "geteuid", None) is None`` branch regardless of host OS.
    """
    monkeypatch.delattr(os, "geteuid", raising=False)
    check = await _check_not_running_as_root(AppConfig())
    assert check.status == "skip"
    assert "geteuid" in check.message


# ----------------------------------------------------------------------
# _detect_container
# ----------------------------------------------------------------------


def test_detect_container_none_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    # Force every filesystem probe to report "absent".
    monkeypatch.setattr(doctor_mod.Path, "exists", lambda self: False)
    assert _detect_container() is None


def test_detect_container_dockerenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    # Compare via as_posix(): Path("/.dockerenv") stringifies with the host
    # separator (backslash on Windows), but the production probe builds the
    # same Path object and calls .exists() on it, so matching on the posix
    # form keeps this assertion host-independent.
    monkeypatch.setattr(
        doctor_mod.Path,
        "exists",
        lambda self: self.as_posix() == "/.dockerenv",
    )
    assert _detect_container() == "/.dockerenv"


def test_detect_container_kubernetes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod.Path, "exists", lambda self: False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert _detect_container() == "KUBERNETES_SERVICE_HOST"


def test_detect_container_cgroup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("12:devices:/docker/abc123\n", encoding="utf-8")

    def fake_exists(self: Path) -> bool:
        return self.as_posix() == "/proc/1/cgroup"

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.as_posix() == "/proc/1/cgroup":
            return cgroup.read_text(encoding="utf-8")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(doctor_mod.Path, "exists", fake_exists)
    monkeypatch.setattr(doctor_mod.Path, "read_text", fake_read_text)
    assert _detect_container() == "cgroup"


# ----------------------------------------------------------------------
# _check_container_sandbox
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_check_skips_outside_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: None)
    check = await _check_container_sandbox(AppConfig())
    assert check.name == "container_sandbox"
    assert check.status == "skip"
    assert "no container" in check.message.lower()


@pytest.mark.asyncio
async def test_sandbox_check_ok_in_container_autodetect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container + default config -> sandbox auto-off, reported ok."""
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: "/.dockerenv")
    cfg = AppConfig(browser={"disable_chromium_sandbox": None})
    check = await _check_container_sandbox(cfg)
    assert check.status == "ok"
    assert "/.dockerenv" in check.message
    assert "--no-sandbox" in check.message


@pytest.mark.asyncio
async def test_sandbox_check_reports_sandbox_on_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator pinned the sandbox ON even inside a container."""
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: "/.dockerenv")
    cfg = AppConfig(browser={"disable_chromium_sandbox": False})
    check = await _check_container_sandbox(cfg)
    assert check.status == "ok"
    assert "sandbox on" in check.message.lower()


@pytest.mark.asyncio
async def test_sandbox_check_reports_off_when_forced_outside_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disable_chromium_sandbox=True with no container is still a skip
    (no container detected) but must report the sandbox as off."""
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: None)
    cfg = AppConfig(browser={"disable_chromium_sandbox": True})
    check = await _check_container_sandbox(cfg)
    assert check.status == "skip"
    assert "off" in check.message.lower()


# ----------------------------------------------------------------------
# Registration in run_doctor
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_checks_registered_in_run_doctor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Non-root + no container so the new checks don't perturb the summary.
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: None)
    report = await run_doctor(AppConfig(base_dir=str(tmp_path)), quick=True)
    names = {c.name for c in report.checks}
    assert "not_running_as_root" in names
    assert "container_sandbox" in names


@pytest.mark.asyncio
async def test_root_warning_does_not_make_report_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Root is a warn, not a fail: it must never flip summary to unusable
    on its own (a fail would block CI via the doctor exit code)."""
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(doctor_mod, "_detect_container", lambda: None)
    report = await run_doctor(AppConfig(base_dir=str(tmp_path)), quick=True)
    root_check = next(c for c in report.checks if c.name == "not_running_as_root")
    assert root_check.status == "warn"
    # Summary may be usable_with_warnings (other optional extras) but never
    # unusable solely because of the root warning.
    if report.summary == "unusable":
        assert any(c.status == "fail" for c in report.checks)
