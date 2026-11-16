"""Tests never touch the user's own TaDaMoo data (profiles, journals, plans, the control secret): they get a folder of their own.

The folder is set before any test module is imported, because the server reads it when its modules load."""
import atexit
import os
import shutil
import tempfile

_DATA = tempfile.mkdtemp(prefix="basalt-test-data-")
os.environ["TADAMOO_DATA"] = _DATA
os.environ["TADAMOO_CREDENTIALS"] = "file"        # never the person's own credential manager
atexit.register(shutil.rmtree, _DATA, True)
os.environ["TADAMOO_DOCLING"] = "off"             # no test starts a Docling reader (a slow model, maybe on the GPU); the ones that do say so with their own command


# Tests written around the one plugin the catalogue used to ship, a private one that is kept out of the repository: they need a plugin in the catalogue and wait for a generic example plugin.
_WAITING = {
    "tests/test_skills_layout.py::test_a_reusable_skill_is_a_standalone_folder_with_its_documents[news-research]",
    "tests/test_skills_layout.py::test_the_modification_skill_is_standalone_and_the_briefing_does_not_depend_on_it",
    "tests/test_skills_layout.py::test_the_modification_skill_is_standalone_and_the_newscast_does_not_depend_on_it",
    "tests/test_skills_layout.py::test_the_plugin_names_the_skills_it_depends_on_and_says_it_brings_no_mcp_server",
    "tests/test_skills_layout.py::test_the_plan_template_is_a_valid_plan_and_nothing_heavy_runs_before_an_approval",
    "tests/test_skills_layout.py::test_the_parameter_files_use_only_parameters_of_the_skill",
    "tests/test_skillmanager.py::test_a_built_in_skill_updates_from_the_program_folder_when_it_changed_there",
    "tests/test_skillmanager.py::test_every_entry_of_the_catalogue_is_a_folder_and_every_top_folder_is_an_entry",
    "tests/test_skillmanager.py::test_what_a_skill_says_it_depends_on_is_what_the_catalogue_requires",
    "tests/test_skillmanager.py::test_a_choice_brings_what_it_requires_once_in_the_order_of_the_catalogue",
    "tests/test_skillmanager.py::test_a_size_is_said_before_the_install_and_models_shared_with_an_installed_skill_count_once",
    "tests/test_skillmanager.py::test_the_plugin_brings_the_skills_it_requires_and_not_the_others",
    "tests/test_skillmanager.py::test_the_imported_copy_of_the_newscast_skill_finds_the_skills_it_uses",
    "tests/test_skillmanager.py::test_the_choice_is_kept_per_skill_in_the_user_data_folder",
    "tests/test_skillmanager.py::test_a_skill_turned_off_is_not_seen_by_the_harness_and_turned_on_it_is_seen_again_with_nothing_downloaded",
    "tests/test_skillmanager.py::test_turning_off_a_skill_with_a_personal_form_puts_it_out_of_the_list_and_back",
    "tests/test_skillmanager.py::test_a_change_made_in_the_capabilities_page_is_followed",
    "tests/test_skillmanager.py::test_skills_imported_before_the_manager_existed_are_taken_in_as_installed",
    "tests/test_skillmanager.py::test_remove_takes_the_copy_out_says_the_models_stay_and_cleans_the_off_list",
    "tests/test_skillmanager.py::test_removing_a_plugin_removes_its_parts_and_a_skill_others_need_says_so",
    "tests/test_skillmanager.py::test_the_shell_lists_installs_turns_off_and_removes",
    "tests/test_skillmanager.py::test_the_setup_installs_the_defaults_or_all_or_the_named_ones_copying_only",
    "tests/test_skills_api.py::test_the_list_has_every_skill_that_ships_with_its_state_size_and_where_things_are",
    "tests/test_skills_api.py::test_installing_is_a_job_that_copies_downloads_only_what_was_asked_and_the_harness_sees_it",
    "tests/test_skills_api.py::test_turning_a_skill_off_and_on_is_seen_by_the_harness_and_downloads_nothing",
    "tests/test_importer.py::test_a_plan_that_already_ran_keeps_its_history_and_asks_first",
    "serve/tests/test_slash_commands.py::ModeLabelWiringTest::test_the_thinking_list_follows_the_conversation",
}


def pytest_collection_modifyitems(config, items):
    import pytest
    skip = pytest.mark.skip(reason="needs a plugin in the catalogue; the one these tests used is a private one kept out of the repository (a generic example plugin is to be made)")
    for item in items:
        if item.nodeid.replace("\\", "/") in _WAITING:
            item.add_marker(skip)
