import argparse
import importlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from my.commands import Command
from my.commands.tests.test_arguments import check_action_attributes
from my.plugins.load import ExternalProcess, Plugin, PluginLoader

if sys.version_info < (3, 10):
    from importlib_metadata import EntryPoint
else:
    from importlib.metadata import EntryPoint


def this_is_a_function():
    return "Definitely!"


def expose_as_entrypoint(name, group, fn) -> EntryPoint:
    assert fn.__name__ not in globals() or globals()[fn.__name__] == fn
    globals()[fn.__name__] = fn
    entrypoint_value = f"{fn.__module__}:{fn.__name__}"
    return EntryPoint(name=name, group=group, value=entrypoint_value)


def test_process_tree_example():
    """An example of a plugin exposing processes with multiple levels"""

    @dataclass
    class A(Command):
        i: int

    plug = Plugin("test_plug", "my.test")
    # Process should be exported as "hello world"
    p1 = ExternalProcess("world", process=A(i=..., cmd="None"), export_path="hello")
    plug.add_process(p1)

    # Process should be exported as "plain"
    p2 = ExternalProcess("plain", process=Command("echo 'this is plain'"), export_path="")
    plug.add_process(p2)

    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--bg", action="store_true")

    plug.add_arguments(parser, process_parser_kwargs={"parents": [common_parser]}, add_all_fields=True)

    expected_arguments = {
        "debug": {"dest": "debug", "option_strings": ["--debug"], "required": False},
        "hello": {
            "world": {
                "bg": {"dest": "bg", "option_strings": ["--bg"], "required": False},
                "i": {"dest": "i", "option_strings": [], "required": True},
            },
            "bg": {"dest": "bg", "option_strings": ["--bg"], "required": False},
        },
        "plain": {"bg": {"dest": "bg", "option_strings": ["--bg"], "required": False}},
    }

    assert check_action_attributes(expected_arguments, parser._actions, excluded=set(["help"]))


def test_entrypoint_creation():
    e = expose_as_entrypoint("name", "my.group", this_is_a_function)
    fn = e.load()
    assert fn() == "Definitely!"


def test_plugin_loader_command():
    @dataclass
    class A(Command):
        i: int

    cmd_entrypoint = expose_as_entrypoint("Printer", "my.plugins.command", A)

    p = PluginLoader()
    p.load_command(cmd_entrypoint)

    assert "test_discover_and_load" in p.plugins
    assert p.plugins["test_discover_and_load"].commands.as_list() == [("Printer", A)]


@pytest.fixture
def example_plugin_installed():
    """Fixture that installs the example plugin and cleans it up after the test."""
    # Get the path to the example plugin
    test_dir = Path(__file__).parent
    repo_root = test_dir.parent.parent.parent
    example_plugin_dir = repo_root / "examples" / "basic"

    # Verify the example plugin directory exists
    assert example_plugin_dir.exists(), f"Example plugin not found at {example_plugin_dir}"

    # Install the plugin in editable mode
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(example_plugin_dir)],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        pytest.fail(f"Failed to install example plugin: {result.stderr}")

    # For editable installs, we need to add the package path to sys.path
    # so it can be imported in the same Python process
    original_sys_path = sys.path.copy()
    if example_plugin_dir not in sys.path:
        sys.path.insert(0, str(example_plugin_dir))

    # Invalidate Python's import caches to make the newly installed package discoverable
    importlib.invalidate_caches()

    # Invalidate the entry points cache to force discovery of the newly installed plugin
    if sys.version_info < (3, 10):
        import importlib_metadata as im
        try:
            im.distributions.cache_clear()
        except AttributeError:
            pass
    else:
        import importlib.metadata as im
        try:
            im.distributions.cache_clear()
        except AttributeError:
            pass

    yield example_plugin_dir

    # Restore sys.path
    sys.path = original_sys_path

    # Cleanup: uninstall the plugin
    subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", "yeti-example"],
        capture_output=True,
        text=True
    )

    # Invalidate cache again after uninstall
    importlib.invalidate_caches()
    if sys.version_info < (3, 10):
        import importlib_metadata as im
        try:
            im.distributions.cache_clear()
        except AttributeError:
            pass
    else:
        import importlib.metadata as im
        try:
            im.distributions.cache_clear()
        except AttributeError:
            pass


def test_example_plugin_discovery(example_plugin_installed):
    """Test that a new plugin (like examples/basic) is discovered by the launcher."""
    # Create a fresh PluginLoader to discover all installed plugins
    loader = PluginLoader()

    # Force fresh discovery by getting entry points from distributions
    # This bypasses any caching issues
    if sys.version_info < (3, 10):
        from importlib_metadata import distributions
    else:
        from importlib.metadata import distributions

    # Manually discover entry points from all distributions
    for dist in distributions():
        if dist.entry_points:
            for ep in dist.entry_points:
                if ep.group == "my.plugins.command":
                    loader.load_command(ep)
                elif ep.group == "my.plugins.process":
                    loader.load_process(ep)
                elif ep.group == "my.plugins.registry":
                    loader.load_registry(ep)

    # Verify that the yeti_example plugin was discovered
    assert "yeti_example" in loader.plugins, (
        f"Plugin 'yeti_example' not found. Available plugins: {list(loader.plugins.keys())}"
    )

    plugin = loader.plugins["yeti_example"]

    # Verify the commands are discovered
    # Commands are registered with the entry points and should include "notes.new" and "notes.list"
    command_list = plugin.commands.as_list()
    command_names = [name for name, _ in command_list]

    assert "notes.new" in command_names or "notes__new" in command_names, (
        f"Command 'notes.new' not found in plugin. Available commands: {command_names}"
    )
    assert "notes.list" in command_names or "notes__list" in command_names, (
        f"Command 'notes.list' not found in plugin. Available commands: {command_names}"
    )

    # Verify the processes are discovered (from the registry)
    # The registry in processes.py should register processes with the same names
    process_list = plugin.processes.as_list()
    process_names = [name for name, _ in process_list]

    # The processes.py registry adds processes at "notes.new" and "notes.list" paths
    assert len(process_list) > 0, "No processes found in the plugin"

    # Check that we can access the processes via the hierarchical structure
    # The registry should expose processes under "notes"
    assert hasattr(plugin.processes, "notes") or any("notes" in str(name) for name in process_names), (
        f"Process hierarchy 'notes' not found. Available processes: {process_names}"
    )
