.PHONY: lint-docs format-docs build-docs serve-docs serve-clean-docs clean-docs codespell build-typedoc

build-typedoc:
	cd libs/sdk-js && yarn install --include-dev && yarn typedoc
	cd libs/sdk-js && yarn --silent concat-md --decrease-title-levels --ignore=js_ts_sdk_ref.md --start-title-level-at 2 docs > ../../docs/docs/cloud/reference/sdk/js_ts_sdk_ref.md 2>/dev/null
	#  Add links to the monorepo
	sed -e '1,10s|@langchain/langgraph-sdk|[@langchain/langgraph-sdk](https://github.com/langchain-ai/langgraph/tree/main/libs/sdk-js)|g' docs/docs/cloud/reference/sdk/js_ts_sdk_ref.md > temp_file && mv temp_file docs/docs/cloud/reference/sdk/js_ts_sdk_ref.md

build-docs: build-typedoc
	poetry run python -m mkdocs build --clean -f docs/mkdocs.yml --strict

serve-clean-docs: clean-docs
	poetry run python -m mkdocs serve -c -f docs/mkdocs.yml --strict -w ./libs/langgraph

serve-docs: build-typedoc
	poetry run python -m mkdocs serve -f docs/mkdocs.yml -w ./libs/langgraph  -w ./libs/checkpoint -w ./libs/sdk-py --dirty

clean-docs:
	find ./docs/docs -name "*.ipynb" -type f -delete
	rm -rf docs/site

## Run format against the project documentation.
format-docs:
	poetry run ruff format docs/docs
	poetry run ruff check --fix docs/docs

# Check the docs for linting violations
lint-docs:
	poetry run ruff format --check docs/docs
	poetry run ruff check docs/docs

codespell:
	./docs/codespell_notebooks.sh .

start-services:
	docker compose -f docs/test-compose.yml up -V --force-recreate --wait --remove-orphans

stop-services:
	docker compose -f docs/test-compose.yml down
