train-models:
	python -m backend.train_models

verify-ml:
	python -c "from backend import ml_predict; ok = ml_predict._load_artifacts(); print('ML READY:', ok); exit(0 if ok else 1)"
