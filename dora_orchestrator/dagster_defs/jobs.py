"""Dagster jobs for Dora orchestration."""

from dagster import define_asset_job

provision_project = define_asset_job("provision_project")

