build:
	docker compose build hamroh

auth: build
	docker compose run --rm codex-auth

auth-status: build
	docker compose run --rm codex-auth python -m hamroh.codex_login --status

up: build
	docker compose up -d hamroh

update:
	./scripts/commit-and-push.sh
	git pull --rebase
	$(MAKE) up

logs:
	docker compose logs -f hamroh

down:
	docker compose down

.PHONY: build auth auth-status up update logs down
