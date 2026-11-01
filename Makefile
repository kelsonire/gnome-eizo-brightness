PYTHON ?= /usr/bin/python3
UUID = eizo-brightness@local

.PHONY: test package shell-test

test:
	$(PYTHON) -m unittest discover -s tests -v
	node --input-type=module --check < extension/extension.js

package:
	mkdir -p dist
	gnome-extensions pack --force --out-dir=dist --extra-source=eizo_brightness.py extension

shell-test: package
	$(PYTHON) tests/run-shell-test.py
