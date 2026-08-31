PY ?= python3
BACKEND := backend

.PHONY: install schema network elevation weather model inspect all serve deck clean help

help:
	@echo "make install     install python dependencies"
	@echo "make schema      create the SQLite schema"
	@echo "make network     build the routable road graph from OSM      (~6 min)"
	@echo "make elevation   fetch DEM elevations + terrain features      (~10 min)"
	@echo "make weather     fetch 2 years of ERA5 rainfall               (~3 min)"
	@echo "make model       train and evaluate the disruption model      (~1 min)"
	@echo "make inspect     explain the model on real NER segments"
	@echo "make all         schema + network + elevation + weather + model"
	@echo "make serve       run the API on 0.0.0.0:8000"
	@echo "make deck        regenerate the pitch deck from live numbers"

install:
	$(PY) -m pip install -r requirements.txt

schema:
	cd $(BACKEND) && $(PY) -m db

network:
	cd $(BACKEND) && $(PY) -u -m pipeline.build_network

elevation:
	cd $(BACKEND) && $(PY) -u -m pipeline.elevation

weather:
	cd $(BACKEND) && $(PY) -u -m pipeline.weather

model:
	cd $(BACKEND) && $(PY) -u -m pipeline.risk_model

inspect:
	cd $(BACKEND) && $(PY) -u -m pipeline.risk_model --inspect

all: schema network elevation weather model

serve:
	cd $(BACKEND) && $(PY) -m uvicorn api.main:app --host 0.0.0.0 --port 8000

deck:
	$(PY) tools/make_deck.py

clean:
	rm -f data/ner_platform.db data/ner_platform.db-* data/processed/*
	@echo "kept data/raw/*.pbf (re-download with 'make network' after deleting them)"
