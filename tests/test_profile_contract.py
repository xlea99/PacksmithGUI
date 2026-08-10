"""The profile's packdump contract (design 3.1) — what counts as "this pack".

`mc_version` is not a property of a profile so much as part of its identity: a dump from a
different Minecraft version is a snapshot of a *different game*, in which the registry ids
the user has tagged may mean something else or not exist at all. So the interesting tests
here are about **refusing**, and about the asymmetry that justifies it — wrongly blocking
costs one explicit override, wrongly accepting silently reinterprets everything the user
owns.
"""
import pathlib

import pytest

from packsmith.core.profile import Profile, mc_versions_compatible


class FakeDump:
    def __init__(self, mc_version="1.20.1", loader="forge", loader_version="47.4.10"):
        self.mc_version = mc_version
        self.loader = loader
        self.loader_version = loader_version


def _profile(tmp_path, **kw):
    """A Profile built directly, so these tests never touch the real profiles dir."""
    settings = kw.pop("settings", {})
    return Profile(name="p", root=tmp_path, mc_path=tmp_path,
                   mc_version=kw.pop("mc_version", "1.20.1"),
                   loader=kw.pop("loader", "forge"),
                   loader_version=kw.pop("loader_version", "47.4.10"),
                   settings=settings)


# --- version comparison -----------------------------------------------------

def test_strict_policy_rejects_any_difference():
    assert mc_versions_compatible("1.20.1", "1.20.1", "strict")
    for other in ("1.20.2", "1.21", "1.21.1", "1.19.4"):
        assert not mc_versions_compatible("1.20.1", other, "strict")


def test_same_minor_allows_patch_bumps_only():
    """The legacy case: 1.7.6 and 1.7.10 genuinely ran the same mods."""
    assert mc_versions_compatible("1.7.6", "1.7.10", "same_minor")
    assert not mc_versions_compatible("1.7.10", "1.8", "same_minor")
    assert not mc_versions_compatible("1.20.1", "1.21.1", "same_minor")


def test_same_minor_gives_up_on_versions_it_cannot_parse():
    """Snapshots and release candidates aren't orderable by our rule, so don't pretend."""
    assert not mc_versions_compatible("1.20.1", "23w31a", "same_minor")
    assert mc_versions_compatible("23w31a", "23w31a", "same_minor")


# --- the contract -----------------------------------------------------------

def test_a_matching_dump_has_no_issues(tmp_path):
    assert _profile(tmp_path).validate_packdump(FakeDump()) == {}


def test_a_version_bump_is_an_error_not_a_warning(tmp_path):
    """This is the case that actually happens — people change Minecraft versions far more
    often than they change loaders."""
    issues = _profile(tmp_path).validate_packdump(FakeDump(mc_version="1.21.1"))
    assert issues["mc_version"]["level"] == "error"
    assert issues["mc_version"]["expected"] == "1.20.1"
    assert issues["mc_version"]["actual"] == "1.21.1"


def test_a_patch_bump_is_an_error_under_the_default_policy(tmp_path):
    """1.20.1 -> 1.20.2 is a parallel mod ecosystem, not an update."""
    issues = _profile(tmp_path).validate_packdump(FakeDump(mc_version="1.20.2"))
    assert issues["mc_version"]["level"] == "error"


def test_a_patch_bump_passes_when_the_profile_opted_into_same_minor(tmp_path):
    profile = _profile(tmp_path, mc_version="1.7.6",
                       settings={"mc_version_policy": "same_minor"})
    assert profile.validate_packdump(FakeDump(mc_version="1.7.10")) == {}


def test_loader_mismatch_is_an_error(tmp_path):
    issues = _profile(tmp_path).validate_packdump(FakeDump(loader="fabric"))
    assert issues["loader"]["level"] == "error"


def test_a_new_forge_build_is_only_informational(tmp_path):
    """A different Forge build for the same Minecraft version is a normal thing to do."""
    issues = _profile(tmp_path).validate_packdump(FakeDump(loader_version="47.4.11"))
    assert issues["loader_version"]["level"] == "info"
    assert not [i for i in issues.values() if i["level"] == "error"]


def test_an_unknown_policy_falls_back_to_strict(tmp_path):
    profile = _profile(tmp_path, settings={"mc_version_policy": "yolo"})
    assert profile.mc_version_policy == "strict"
    assert profile.validate_packdump(FakeDump(mc_version="1.20.2"))


# --- adopting a contract ----------------------------------------------------

def test_a_profile_with_no_version_has_no_contract_until_it_adopts_one(tmp_path):
    profile = _profile(tmp_path, mc_version=None, loader=None, loader_version=None)
    assert profile.validate_packdump(FakeDump(mc_version="1.21.1", loader="fabric")) == {}

    profile.save = lambda: None          # don't write to disk in a unit test
    adopted = profile.adopt_contract_from(FakeDump())
    assert sorted(adopted) == ["loader", "loader_version", "mc_version"]
    assert profile.mc_version == "1.20.1"
    # ...and now it does have one.
    assert profile.validate_packdump(FakeDump(mc_version="1.21.1"))["mc_version"]["level"] \
        == "error"


def test_adopting_never_overwrites_a_declared_field(tmp_path):
    profile = _profile(tmp_path, loader_version=None)
    profile.save = lambda: None
    assert profile.adopt_contract_from(FakeDump(mc_version="1.21.1")) == ["loader_version"]
    assert profile.mc_version == "1.20.1"      # untouched


# --- launching with nothing to work on (design 3.1) -------------------------

def test_no_developer_home_directory_is_baked_into_the_source():
    """There was a fallback that CREATED a profile pointing at one developer's home
    directory — a guaranteed launch crash on every other machine.

    Deliberately a grep, because the value is that it cannot drift. It looks for a HOME
    directory, not for "curseforge" -- a placeholder path shown as a hint in a dialog
    in a dialog is a helpful hint, and nothing reads it.
    """
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "packsmith"
    pattern = re.compile(r"[A-Za-z]:.{0,4}[Uu]sers.{0,4}\w+", re.ASCII)
    offenders = sorted(
        str(f.relative_to(root)) for f in root.rglob("*.py")
        if pattern.search(f.read_text(encoding="utf-8"))
    )
    assert not offenders, f"machine-specific paths in: {offenders}"
