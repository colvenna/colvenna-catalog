"""UAT-DEP-03 Run again.

The install recorded (its stacks and each resource's physical id, the Helm release and its values,
the pods and their UIDs or the functions' versions and the alias, the repositories' images, the
admin token's version, the principals, grants and tables), install.sh --yes run again with the same
configuration and candidate, and recorded again: nothing made or replaced, nothing restarted,
copied or built, the token not issued again, the data and grants as they were."""

from uatlib import deploy

ID = "UAT-DEP-03"
FORMS = ("eks", "serverless")


def checks(form, args=None):
    return ["install-exit", "every-step-done", "nothing-made-or-replaced", "no-restart", "no-image-copied-or-built",
            "admin-token-kept", "data-and-grants-kept"]


def add_arguments(parser):
    deploy.add_install_arguments(parser)


def judge(form, nlb, run, before, after):
    """{check: (ok, detail)} of a rerun, from its output and the records before and after (UAT-DEP-05's
    flag-overrides-refusal judges its run the same way)."""
    ok, detail = deploy.every_step_done(run, form, nlb)
    changes = deploy.compare_resources(before["stacks"], after["stacks"])
    results = {"install-exit": deploy.judge_exit(run),
               "every-step-done": (ok, detail),
               "nothing-made-or-replaced": (not changes and set(before["stacks"]) == set(after["stacks"]),
                                            f"{len(after['stacks'])} stacks; changes: {changes or 'none'}")}
    if form == "eks":
        b = [(p["name"], p["uid"]) for p in before["kube"]["pods"]]
        a = [(p["name"], p["uid"]) for p in after["kube"]["pods"]]
        helm_b, helm_a = before["kube"]["helm"], after["kube"]["helm"]
        same_values = before["kube"]["values"] == after["kube"]["values"]
        # Every pod the Deployment asks for, so that none was missed while one was replaced.
        whole = len(a) == after["kube"].get("replicas", len(a))
        results["no-restart"] = (b == a and whole and same_values,
                                 f"pods {'the same' if b == a else f'{b} then {a}'}; Helm revision "
                                 f"{helm_b and helm_b['revision']} then {helm_a and helm_a['revision']}; values the same: {same_values}")
    else:
        fb, fa = before["functions"].get("alias"), after["functions"].get("alias")
        results["no-restart"] = (fb is not None and fb == fa, f"alias live on version {fb} then {fa}")
    results["no-image-copied-or-built"] = (deploy.kept(before, after, "repositories"),
                                           f"repositories {sorted(before['repositories'])}: "
                                           f"{'the same images' if deploy.kept(before, after, 'repositories') else 'changed'}")
    results["admin-token-kept"] = (before.get("adminToken") and deploy.kept(before, after, "adminToken"),
                                   f"version {before.get('adminToken')} then {after.get('adminToken')}")
    same = all(deploy.kept(before, after, k) for k in ("principals", "grants", "tables"))
    results["data-and-grants-kept"] = (same, f"{len(after['principals'])} principals, {len(after['grants'])} grants, "
                                             f"{sum(len(t) for t in after['tables'].values())} tables: "
                                             f"{'as recorded' if same else 'changed'}")
    return results


def run(ctx):
    args = ctx.args
    with ctx.preparing("the install there, with a table, a principal and a grant of the case's own; recorded") as notes:
        # Something of the case's own to find again: a database with a table, a principal with a grant.
        db, who = f"dep03_{ctx.run}", f"dep03-{ctx.run}"
        rest, admin = ctx.rest, ctx.admin
        rest.create_database(db)
        ctx.on_cleanup(f"database {db} dropped", lambda: rest.drop_database(db) and "dropped")
        rest.create_table(db, "t", [("id", "INT")])
        admin.principal(who, ID)
        admin.grant(who, "TABLE", "READ", database=db, obj="t")
        before = deploy.record_install(ctx)
        ctx.keep("before.json", before)
        if not before["stacks"]:
            raise RuntimeError("no stack of the install")
        notes.append(f"{len(before['stacks'])} stacks, {len(before['principals'])} principals, {len(before['grants'])} grants")
    installer = deploy.Installer(ctx, deploy.checkout_of(args), args.config)
    run = installer.run("install.sh", *deploy.install_args(args), "--yes", keep="install-again")
    after = deploy.record_install(ctx)
    ctx.keep("after.json", after)
    for name, (ok, detail) in judge(ctx.form, ctx.install.get("COLVENNA_INTERNAL_NLB") == "on", run, before, after).items():
        ctx.check(name, ok, detail)
