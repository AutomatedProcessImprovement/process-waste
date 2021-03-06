#!/usr/bin/env bash

rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install poetry
pip install -e .
cd vendor/start-time-estimator; pip install -e .; cd ../..
cd vendor/batch-processing-analysis; pip install -e .; cd ../..
cd vendor/Prosimos; pip install -e .; cd ../..
poetry build