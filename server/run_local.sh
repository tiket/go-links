#!/bin/bash

export DATABASE=postgres
export ENVIRONMENT=prod

cd src

export FLASK_APP=main.py
export FLASK_RUN_PORT=9095
export FLASK_ENV=production

flask db upgrade

flask run
