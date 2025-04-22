from __future__ import annotations

import contextlib
import re
import subprocess
import sys
from datetime import datetime
from importlib import import_module
from pathlib import Path

from django.conf import settings

from common.command import BaseCommand

BASE_DIR = Path(settings.BASE_DIR)


def list_migrations():
    app_migrations = {}
    app_head_migrations = {}
    print("list migrations")
    for migration_dir in BASE_DIR.glob("*/migrations"):
        assert (migration_dir / "__init__.py").exists()
        app = migration_dir.parent.name
        print(f"    {app}")
        migrations = sorted(
            p.stem for p in migration_dir.glob("*.py") if p.stem != "__init__"
        )
        for m in migrations:
            print(f"        {m}")
        app_migrations[app] = migrations
        max_migration = BASE_DIR / get_max_migration_path(app)
        head = max_migration.read_text().strip()
        print(f"        HEAD: {head}")
        app_head_migrations[app] = head
    print()
    return app_migrations, app_head_migrations


def list_model_files():
    return set(BASE_DIR.glob("**/models/**/*.py")) | set(BASE_DIR.glob("**/models.py"))


def run(*cmd, **kwargs):
    flat = " ".join(str(x) for x in cmd)
    if len(flat) > 100:
        flat = flat[:96] + " ..."
    print("   ", flat)
    subprocess.run(cmd, check=True, **kwargs)


@contextlib.contextmanager
def Replacer(auto_revert=True):
    to_restore = set()

    def replace(replacements, *files, optional=False):
        print("Replacing")
        for old, new in replacements:
            print(f"    {old!r} -> {new!r}")
        print("In")
        for file in files:
            assert isinstance(file, Path)
            print(f"    {file}")
            to_restore.add(file)
            content = file.read_text()
            for replacement in replacements:
                content, num_subs = re.subn(*replacement, content)
                assert optional or num_subs
            file.write_text(content)

    yield replace

    if auto_revert:
        print("Restoring")
        run("git", "checkout", *to_restore)
        print()


def get_migration_path(app, migration, ext="py"):
    return f"{app}/migrations/{migration}.{ext}"


def get_max_migration_path(app):
    return get_migration_path(app, "max_migration", ext="txt")


def get_dep_entry(app, migration):
    return f'("{app}", "{migration}")'


def crush_migrations():
    current_migrations, current_head_migrations = list_migrations()

    # check for apps that only have an initial migration
    # we can't handle this currently
    passed = True
    for app, migrations in current_migrations.items():
        if len(migrations) < 2:
            passed = False
            print(f"{app} has insufficient migrations for crushing")
            print("find an excuse to make another one")
    if not passed:
        return

    # check for existing squash files (contains replaces)
    # they will need cleaning up before we crush
    bad_apps = set()
    for app, migrations in current_migrations.items():
        for migration in migrations:
            name = f"{app}.migrations.{migration}"
            mod = import_module(name)
            if mod.Migration.replaces:
                bad_apps.add(app)
                print(f"Existing squash {name}")
    if bad_apps:
        print()
        print("Can't crush with existing squashes, clean them up first")
        print("delete_squashed_migrations is whitespace sensitive")
        print("and doesn't handle cross app dependencies well")
        print("so we run it once to delete the files")
        print("then we reformat to very long lines")
        print("and then run it again to remove the 'replaces=' lines")
        print("then we reformat back to normal")
        print("")
        for app in sorted(bad_apps):
            print(f"python manage.py delete_squashed_migrations {app} --noinput")
        print("black -l 1000000 */migrations/*.py")
        for app in sorted(bad_apps):
            print(f"python manage.py delete_squashed_migrations {app} --noinput")
        print("black */migrations/*.py")
        return

    # delete all migration files to get them out of the way
    for app, migrations in current_migrations.items():
        for migration in migrations:
            (BASE_DIR / get_migration_path(app, migration)).unlink()

    with Replacer() as replace:
        # hack common.models to get rid of foreignkeys
        # do this by converting `ForeignKey = ...` into
        # `ForeignKey = lambda *args, **kwargs: None and ...` etc
        replace(
            [
                (r"ForeignKey = ", "ForeignKey = lambda *args, **kwargs: None and "),
                (
                    r"OneToOneField = ",
                    "OneToOneField = lambda *args, **kwargs: None and ",
                ),
                (
                    r"ManyToManyField = ",
                    "ManyToManyField = lambda *args, **kwargs: None and ",
                ),
                (  # hackery to make posthog_ext go (it has no FK's)
                    r"PositiveIntegerField = ",
                    "PositiveIntegerField = lambda *args, **kwargs: None and ",
                ),
                (r"class FirmField\(ForeignKey\):", "FirmField = ForeignKey\nclass X:"),
            ],
            BASE_DIR / "common/models.py",
        )

        # hack all models to get rid of indicies, constraints and uniques
        # do this by converting `indicies = [...]` into `indicies = [] and [...]` etc
        model_files = list_model_files()
        replace(
            [
                (r"        indexes = \[", "        indexes = [] and ["),
                (r"        constraints = \[", "        constraints = [] and ["),
                (r"        unique_together = \[", "        unique_together = [] and ["),
                (r"        indexes = \(", "        indexes = () and ("),
                (r"        constraints = \(", "        constraints = () and ("),
                (r"        unique_together = \(", "        unique_together = () and ("),
            ],
            *model_files,
            optional=True,
        )

        # hack the admin app to prevent the admins registering so they don't get upset
        # about fields we've removed not existing
        replace(
            [
                (
                    r"from django.contrib.admin.apps import AdminConfig",
                    "from django.apps import AppConfig as AdminConfig",
                ),
                (r"def ready", "def xready"),
            ],
            BASE_DIR / "admin/apps.py",
        )

        # generate initial migrations (reuse existing name)
        for app, migrations in current_migrations.items():
            print(f"Generating initial migration for {app}")
            first_migration = migrations[0]
            assert first_migration.startswith("0001_")
            migration_name = first_migration[5:]
            run(
                sys.executable, "manage.py", "makemigrations", app, "-n", migration_name
            )
            print()

    # generate squash migrations (unique name)
    squash_name = datetime.now().strftime("squashed_on_%Y%m%d_%H%M%S")
    print("Generating squash migrations")
    run(
        sys.executable,
        "manage.py",
        "makemigrations",
        "-n",
        squash_name,
        input=b"1\n0\n" * 100000,  # interactively tell Django to default it all to 0
    )
    print()

    # add replaces to the squash migrations
    print("Add replaces to squash migrations")
    for app, migrations in current_migrations.items():
        print(f"    {app}")
        pattern = get_migration_path(app, f"*_{squash_name}")
        path = next(BASE_DIR.glob(pattern))
        with path.open("a") as f:
            f.write("    replaces = [\n")
            for migration in migrations[1:]:
                dep = get_dep_entry(app, migration)
                f.write(f"        {dep},\n")
            f.write("    ]\n")
    print()

    # rewrite dependencies
    print("Rewrite dependencies")
    new_migrations, new_head_migrations = list_migrations()
    replacements = []
    for app, migrations in new_migrations.items():
        assert len(migrations) == 2, migrations
        assert migrations[0].startswith("0001_")
        old_dep = get_dep_entry(app, migrations[1])
        new_dep = get_dep_entry(app, migrations[0])
        replacements.append((old_dep, new_dep))
    with Replacer(auto_revert=False) as replace:
        for app, migrations in new_migrations.items():
            replace(
                replacements,
                *[BASE_DIR / get_migration_path(app, m) for m in migrations],
                optional=True,
            )
    print()

    # renumber squash migrations to preserve max migration number
    print("Renaming squash migrations")
    for app, orig_head in current_head_migrations.items():
        existing_head = new_head_migrations[app]
        new_head = orig_head[:4] + existing_head[4:]
        print(f"    {existing_head} -> {new_head}")
        existing_path = BASE_DIR / get_migration_path(app, existing_head)
        new_path = BASE_DIR / get_migration_path(app, new_head)
        max_migration_path = BASE_DIR / get_migration_path(app, "max_migration", "txt")
        existing_path.rename(new_path)
        max_migration_path.write_text(f"{new_head}\n")
    print()

    # restore migration files (except initial)
    print("Restore migrations")
    for app, migrations in current_migrations.items():
        paths = [str(BASE_DIR / get_migration_path(app, m)) for m in migrations[1:]]
        run("git", "checkout", *paths)
    print()


class Command(BaseCommand):
    def handle(self, *args, **options):
        crush_migrations()
