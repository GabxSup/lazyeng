.PHONY: setup run test docker docker-down clean
setup:
	./setup.sh
run:
	./run.sh
test:
	PYTHONPYCACHEPREFIX=/tmp/aurum-pyc PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
docker:
	docker compose up --build
docker-down:
	docker compose down
clean:
	rm -rf data __pycache__ app/__pycache__ tests/__pycache__
