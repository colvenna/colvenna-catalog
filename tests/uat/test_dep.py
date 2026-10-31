"""The deployment cases' judges and their runner of install.sh, offline: a stand-in install.sh
that prints what the real one prints, and records of stacks and inventories as the AWS CLI gives them."""

import signal
import subprocess
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

from cases import dep03, dep05, dep08, dep09
from uatlib import deploy

EKS_OUTPUT = """== tools
== preflight
Colvenna Catalog install
  account   <ACCOUNT_ID>
  region    us-west-2
  cluster   c (namespace colvenna-catalog, release colvenna-catalog)
  version   release v0.0.0-rc.9 (built from abc; image and chart by digest from <registry>)
  bucket    <a new one> (prefix warehouse)
  database  <a new RDS PostgreSQL 16> (colvenna_catalog)
  internal NLB off
Install? Type the environment name (ek1) to go on:
== stacks
== image
== namespace
== database
== tls
== chart
== admin
== smoke
client namespace: anonymous=401 admin=200
other namespace: anonymous=000 admin=000"""


def run_of(text, code=0):
    lines = [(f"2026-10-29T00:00:{i:02d}.000Z", t) for i, t in enumerate(text.splitlines())]
    return deploy.Run(command=["install.sh"], exit_code=code, lines=lines, started="2026-10-29T00:00:00.000Z")


class Judges(unittest.TestCase):
    def test_every_step_in_order_and_the_smoke_check(self):
        self.assertTrue(deploy.every_step_done(run_of(EKS_OUTPUT), "eks")[0])
        self.assertFalse(deploy.every_step_done(run_of(EKS_OUTPUT.replace("== tls\n", "")), "eks")[0])
        failed = EKS_OUTPUT.replace("== admin", 'install.sh: failed at step "chart" (status 1); fix the cause and run it again')
        self.assertFalse(deploy.every_step_done(run_of(failed, 1), "eks")[0])
        self.assertEqual(run_of(failed, 1).failed_step(), ("chart", 1))
        nlb = EKS_OUTPUT.replace("== admin", "== internal NLB\n== admin")
        self.assertTrue(deploy.every_step_done(run_of(nlb), "eks", nlb=True)[0])

    def test_the_summary_and_the_question_before_the_first_change(self):
        ok, _ = deploy.summary_before_change(run_of(EKS_OUTPUT), "eks", "v0.0.0-rc.9", "abc")
        self.assertTrue(ok)
        late = EKS_OUTPUT.replace("Install? Type the environment name (ek1) to go on:\n== stacks", "== stacks\nInstall? Type the environment name (ek1) to go on:")
        self.assertFalse(deploy.summary_before_change(run_of(late), "eks")[0])
        self.assertFalse(deploy.summary_before_change(run_of(EKS_OUTPUT), "eks", "v0.0.0-rc.8")[0])

    def test_a_token_in_any_text(self):
        token = "colvenna_" + "A" * 43
        self.assertEqual(deploy.token_leaks({"a": "fine", "b": f"Bearer {token}"}), ["b"])
        self.assertEqual(deploy.token_leaks({"a": "the secret xyz123"}, ["xyz123"]), ["a"])
        self.assertEqual(deploy.token_leaks({"metrics": "colvenna_catalog_requests_total 3"}), [])

    def test_resources_made_replaced_or_gone(self):
        before = {"S": {"resources": {"A": ["T", "a1"], "B": ["T", "b1"]}}}
        self.assertEqual(deploy.compare_resources(before, before), [])
        after = {"S": {"resources": {"A": ["T", "a2"], "C": ["T", "c1"]}}, "N": {"resources": {}}}
        self.assertEqual(deploy.compare_resources(before, after), ["N: made", "S/A: replaced", "S/B: gone", "S/C: made"])

    def test_stacks_made_after_the_run_began(self):
        stacks = {"X": {"created": "2026-10-29T00:00:05.000Z"}}
        self.assertTrue(deploy.created_after(stacks, "2026-10-29T00:00:00.000Z")[0])
        self.assertFalse(deploy.created_after(stacks, "2026-10-29T00:01:00.000Z")[0])
        self.assertFalse(deploy.created_after({}, "2026-10-29T00:00:00.000Z")[0])

    def test_what_is_the_installs_and_what_is_not(self):
        mine = ["ColvennaCatalogEk1Storage", "colvenna-catalog-ek1/colvenna-catalog", "colvennacatalogek1storage-bucket1",
                "/aws/lambda/ColvennaCatalogEk1Storag-LogRetention", "/aws/rds/instance/colvennacatalogek1storage-db/x"]
        theirs = ["ColvennaCatalogEk1xStorage", "colvenna-catalog-ek1x-test-driver", "ColvennaCatalogTestU1",
                  "ColvennaCatalogRelease", "colvennacatalogek1xstorage-b"]
        self.assertTrue(all(deploy.of_install(i, "ek1") for i in mine))
        self.assertFalse(any(deploy.of_install(i, "ek1") for i in theirs))
        # A long environment name: CloudFormation cut the stack's name within its component, as
        # uninstall.sh's rule (deploy/lib/stacks.sh) takes it.
        cut = "/aws/lambda/ColvennaCatalogAbcdefStor-LogRetentionaae0aa3c5b4d-AbCdEfGhIjKl"  # pragma: allowlist secret - a made-up CloudFormation-cut Lambda log group name of the test, not a secret
        self.assertTrue(deploy.of_install(cut, "abcdef"))
        self.assertTrue(deploy.of_install("/aws/lambda/ColvennaCatalogAbcdefghSt-X-Y", "abcdefgh"))
        self.assertFalse(deploy.of_install(cut, "abcde"))
        self.assertFalse(deploy.of_install("/aws/lambda/ColvennaCatalogAbcdefS-X-Y", "abcdef"))  # 22: too short a cut
        self.assertEqual(deploy.env_of(cut), "abcdef")
        self.assertEqual(deploy.env_of("/aws/lambda/ColvennaCatalogAbcdefghSt-X-Y"), "abcdefgh")

    def test_others_untouched_and_the_shared_account(self):
        before = {"stacks": {"ColvennaCatalogTestU1": ["UPDATE_COMPLETE", "t1"], "ColvennaCatalogRelease": ["UPDATE_COMPLETE", "t0"],
                             "ColvennaCatalogEk1Eks": ["CREATE_COMPLETE", "t2"], "ColvennaCatalogOtherEks": ["CREATE_COMPLETE", "t3"]},
                  "buckets": ["colvennacatalogek1storage-x", "colvennacatalogotherstorage-y"]}
        after = {"stacks": {"ColvennaCatalogTestU1": ["UPDATE_COMPLETE", "t1"], "ColvennaCatalogRelease": ["UPDATE_COMPLETE", "t0"],
                            "ColvennaCatalogOtherEks": ["UPDATE_COMPLETE", "t9"], "ColvennaCatalogNewEks": ["CREATE_COMPLETE", "t4"]},
                 "buckets": ["colvennacatalogotherstorage-y"]}
        # The install's own gone, another's changed by its own work and a new one made: untouched.
        self.assertTrue(deploy.others_untouched(before, after, "ek1", "ColvennaCatalogTestU1")[0])
        gone = dict(after, buckets=[])
        self.assertFalse(deploy.others_untouched(before, gone, "ek1", "ColvennaCatalogTestU1")[0])
        # Another install uninstalled meanwhile by its own run took its resources with it; one whose
        # stacks stay but whose function went was touched.
        both = {"stacks": dict(before["stacks"], ColvennaCatalogSl2Serverless=["CREATE_COMPLETE", "t5"]),
                "functions": ["colvenna-catalog-sl2-server", "colvenna-catalog-sl3-server"]}
        later = {"stacks": {k: v for k, v in both["stacks"].items() if k != "ColvennaCatalogSl2Serverless"},
                 "functions": []}
        ok, detail = deploy.others_untouched(both, later, "ek1", "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIn("colvenna-catalog-sl3-server", detail)
        self.assertNotIn("sl2-server", detail.split("(")[0])
        self.assertEqual(deploy.env_of("/aws/lambda/ColvennaCatalogSn52dStorag-LogRetention"), "sn52d")
        self.assertEqual(deploy.env_of("colvennacatalogek52dstorage-warehouse"), "ek52d")
        self.assertIsNone(deploy.env_of("ColvennaCatalogTestU52d"))
        self.assertEqual(deploy.env_of("ColvennaCatalogTestSn52dDriver"), "sn52d")
        self.assertTrue(deploy.of_install("ColvennaCatalogTestSn52dDriver", "sn52d"))
        # VPC endpoints are of the stack that made them: the install's own go with it.
        ends = {"stacks": {}, "endpoints": ["ColvennaCatalogSn52dNetwork/vpce-1", "ColvennaCatalogTestU52d/vpce-2", "-/vpce-3"]}
        self.assertEqual(deploy.own(ends, "sn52d")["endpoints"], ["ColvennaCatalogSn52dNetwork/vpce-1"])
        self.assertTrue(deploy.others_untouched(ends, dict(ends, endpoints=ends["endpoints"][1:]), "sn52d", "ColvennaCatalogTestU52d")[0])
        self.assertFalse(deploy.others_untouched(ends, dict(ends, endpoints=ends["endpoints"][:2]), "sn52d", "ColvennaCatalogTestU52d")[0])
        self.assertFalse(deploy.of_install("ColvennaCatalogTestU52d", "u52d"))
        # One being uninstalled meanwhile (its stack still deleting) is that run's too, with its driver.
        deleting = {"stacks": dict(both["stacks"], ColvennaCatalogTestSl2Driver=["DELETE_IN_PROGRESS", "t6"]),
                    "functions": ["colvenna-catalog-sl2-server"]}
        meanwhile = {"stacks": dict(both["stacks"], ColvennaCatalogSl2Serverless=["DELETE_IN_PROGRESS", "t5"]), "functions": []}
        self.assertTrue(deploy.others_untouched(deleting, meanwhile, "ek1", "ColvennaCatalogTestU1")[0])
        changed = dict(after, stacks=dict(after["stacks"], ColvennaCatalogTestU1=["UPDATE_COMPLETE", "t8"]))
        self.assertFalse(deploy.others_untouched(before, changed, "ek1", "ColvennaCatalogTestU1")[0])
        # An install removed meanwhile is not excused when the case's own scripts named its stacks.
        alone = {"stacks": before["stacks"], "functions": ["colvenna-catalog-sl2-server"]}
        alone["stacks"] = dict(alone["stacks"], ColvennaCatalogSl2Serverless=["CREATE_COMPLETE", "t5"])
        gone_too = {"stacks": before["stacks"], "functions": []}
        self.assertTrue(deploy.others_untouched(alone, gone_too, "ek1", "ColvennaCatalogTestU1", said="deleting ColvennaCatalogEk1Eks")[0])
        ok, detail = deploy.others_untouched(alone, gone_too, "ek1", "ColvennaCatalogTestU1", said="deleting ColvennaCatalogSl2Serverless")
        self.assertFalse(ok)
        self.assertIn("sl2", detail)

    def test_an_install_of_another_region(self):
        """#46: the account lists roles and buckets whatever the region, so an install of another region is in
        this region's inventory by them; its stacks there say whether its own run removed it meanwhile."""
        here = {"ColvennaCatalogTestU1": ["UPDATE_COMPLETE", "t1"], "ColvennaCatalogSx1Serverless": ["CREATE_COMPLETE", "t2"]}
        there = {"ColvennaCatalogEx2Eks": ["CREATE_COMPLETE", "t3", "us-west-2"], "ColvennaCatalogTestU2": ["UPDATE_COMPLETE", "t0", "us-west-2"]}
        roles = ["colvenna-catalog-ex2-data", "colvenna-catalog-ex2-server"]
        before = {"stacks": here, "stacksElsewhere": there, "roles": roles}
        # Uninstalled meanwhile by its own run in its region: its roles went with it.
        gone = {"stacks": here, "stacksElsewhere": {"ColvennaCatalogTestU2": there["ColvennaCatalogTestU2"]}, "roles": []}
        ok, detail = deploy.others_untouched(before, gone, "sx1", "ColvennaCatalogTestU1")
        self.assertTrue(ok, detail)
        self.assertIn("uninstalled meanwhile by their own runs: ['ex2']", detail)
        deleting = {"stacks": here, "stacksElsewhere": dict(there, ColvennaCatalogEx2Eks=["DELETE_IN_PROGRESS", "t4", "us-west-2"]),
                    "roles": roles[:1]}
        self.assertTrue(deploy.others_untouched(before, deleting, "sx1", "ColvennaCatalogTestU1")[0])
        # Its stacks still there but its roles gone: touched.
        ok, detail = deploy.others_untouched(before, {"stacks": here, "stacksElsewhere": there, "roles": []}, "sx1",
                                             "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIn("colvenna-catalog-ex2-server", detail)
        # Another region's test environment gone is no install's doing.
        self.assertFalse(deploy.others_untouched(before, dict(before, stacksElsewhere={"ColvennaCatalogEx2Eks": there["ColvennaCatalogEx2Eks"]}),
                                                 "sx1", "ColvennaCatalogTestU1")[0])
        # An inventory without the other regions' stacks judges as before.
        self.assertTrue(deploy.others_untouched({"stacks": here}, {"stacks": here}, "sx1", "ColvennaCatalogTestU1")[0])

    def test_an_install_uninstalled_and_made_again_meanwhile_by_its_own_runs(self):
        """#62: another install deleted (--delete-data) and installed again under the same names while the case ran:
        its stacks are there before and after, but they are other stacks (their ids differ), so its run removed
        the first install's buckets and instances, as an uninstall does (#46), not this case."""
        u1 = ["UPDATE_COMPLETE", "t1", "11111111-1111-4111-8111-111111111111"]
        before = {"stacks": {"ColvennaCatalogTestU1": u1,
                             "ColvennaCatalogEu2Storage": ["CREATE_COMPLETE", "t2", "22222222-2222-4222-8222-222222222222"],
                             "ColvennaCatalogEu2Eks": ["CREATE_COMPLETE", "t2", "33333333-3333-4333-8333-333333333333"]},
                  "stacksElsewhere": {"ColvennaCatalogEu3Storage": ["CREATE_COMPLETE", "t2", "44444444-4444-4444-8444-444444444444",
                                                                    "us-east-2"]},
                  "buckets": ["colvennacatalogeu2storage-warehouse-a", "colvennacatalogeu3storage-warehouse-c"],
                  "databases": ["colvennacatalogeu2storage-db-a available"]}
        remade = {"stacks": {"ColvennaCatalogTestU1": u1,
                             "ColvennaCatalogEu2Storage": ["CREATE_COMPLETE", "t5", "55555555-5555-4555-8555-555555555555"],
                             "ColvennaCatalogEu2Eks": ["CREATE_COMPLETE", "t5", "66666666-6666-4666-8666-666666666666"]},
                  "stacksElsewhere": {"ColvennaCatalogEu3Storage": ["CREATE_COMPLETE", "t5", "77777777-7777-4777-8777-777777777777",
                                                                    "us-east-2"]},
                  "buckets": ["colvennacatalogeu2storage-warehouse-b", "colvennacatalogeu3storage-warehouse-d"],
                  "databases": ["colvennacatalogeu2storage-db-b available"]}
        ok, detail = deploy.others_untouched(before, remade, "sx1", "ColvennaCatalogTestU1")
        self.assertTrue(ok, detail)
        self.assertIn("uninstalled meanwhile by their own runs: ['eu2', 'eu3']", detail)
        self.assertIn("installed again under the same names: ['eu2', 'eu3']", detail)
        # The same stacks (their ids kept), merely updated: their data still may not go (#52).
        updated = dict(remade, stacks={k: v[:2] + before["stacks"][k][2:] for k, v in remade["stacks"].items()},
                       stacksElsewhere={k: v[:2] + before["stacksElsewhere"][k][2:] for k, v in remade["stacksElsewhere"].items()})
        ok, detail = deploy.others_untouched(before, updated, "sx1", "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIn("buckets gone", detail)
        self.assertIn("databases gone", detail)
        # A record made before the ids were (two fields; three with the region elsewhere) judges as before.
        old = {k: {s: v[:2] + v[3:] for s, v in before[k].items()} if k.startswith("stacks") else v for k, v in before.items()}
        ok, _ = deploy.others_untouched(old, {k: {s: v[:2] + v[3:] for s, v in remade[k].items()} if k.startswith("stacks") else v
                                              for k, v in remade.items()}, "sx1", "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIsNone(deploy.stack_uuid(["CREATE_COMPLETE", "t2", "us-east-2"]))
        # Its test driver made again, and nothing else: its data still may not go.
        driver = dict(before, stacks=dict(before["stacks"], ColvennaCatalogTestEu2Driver=["CREATE_COMPLETE", "t2",
                                                                                         "88888888-8888-4888-8888-888888888888"]))
        redriven = dict(updated, stacks=dict(updated["stacks"], ColvennaCatalogTestEu2Driver=["CREATE_COMPLETE", "t6",
                                                                                            "99999999-9999-4999-8999-999999999999"]))
        ok, detail = deploy.others_untouched(driver, redriven, "sx1", "ColvennaCatalogTestU1")
        self.assertFalse(ok, detail)
        self.assertIn("colvennacatalogeu2storage-warehouse-a", detail)
        # The case's own scripts naming that install's stacks are no excuse.
        ok, detail = deploy.others_untouched(before, remade, "sx1", "ColvennaCatalogTestU1", said="ColvennaCatalogEu2Storage")
        self.assertFalse(ok)

    def test_the_account_s_stacks_carry_their_ids(self):
        class Aws:
            def __call__(self, *args):
                return [{"StackName": "ColvennaCatalogEu2Storage", "StackStatus": "CREATE_COMPLETE", "CreationTime": "t2",
                         "StackId": "arn:aws:cloudformation:us-west-2:111122223333:stack/ColvennaCatalogEu2Storage/"
                                    "22222222-2222-4222-8222-222222222222"}]
        self.assertEqual(deploy.account_stacks(Aws()),
                         {"ColvennaCatalogEu2Storage": ["CREATE_COMPLETE", "t2", "22222222-2222-4222-8222-222222222222"]})

    def test_an_install_updated_meanwhile_by_its_own_run(self):
        """#52: another install's stacks updated meanwhile (UAT-DEP-07 taking it back to a release without its
        purge function) changed its own resources; one whose stacks did not change was touched."""
        before = {"stacks": {"ColvennaCatalogTestU1": ["UPDATE_COMPLETE", "t1"],
                             "ColvennaCatalogSu2Serverless": ["UPDATE_COMPLETE", "t2"]},
                  "functions": ["colvenna-catalog-su2-purge", "colvenna-catalog-su2-server"],
                  "roles": ["colvenna-catalog-su2-purge"]}
        back = {"stacks": dict(before["stacks"], ColvennaCatalogSu2Serverless=["UPDATE_COMPLETE", "t3"]),
                "functions": ["colvenna-catalog-su2-server"], "roles": []}
        ok, detail = deploy.others_untouched(before, back, "sx3", "ColvennaCatalogTestU1")
        self.assertTrue(ok, detail)
        self.assertIn("updated meanwhile by their own runs: ['su2']", detail)
        going = dict(back, stacks=dict(before["stacks"], ColvennaCatalogSu2Serverless=["UPDATE_IN_PROGRESS", "t3"]))
        self.assertTrue(deploy.others_untouched(before, going, "sx3", "ColvennaCatalogTestU1")[0])
        # An update never takes an install's data: its database gone was touched.
        lost = dict(back, databases=[])
        ok, detail = deploy.others_untouched(dict(before, databases=["colvennacatalogsu2storage-db available"]), lost, "sx3",
                                             "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIn("databases gone", detail)
        # Its stacks as they were: its function gone was touched.
        ok, detail = deploy.others_untouched(before, dict(back, stacks=before["stacks"]), "sx3", "ColvennaCatalogTestU1")
        self.assertFalse(ok)
        self.assertIn("colvenna-catalog-su2-purge", detail)
        # Its test driver redeployed is no update of the install.
        driver = {"stacks": dict(before["stacks"], ColvennaCatalogTestSu2Driver=["CREATE_COMPLETE", "t5"]),
                  "functions": before["functions"], "roles": before["roles"]}
        redeployed = dict(back, stacks=dict(driver["stacks"], ColvennaCatalogTestSu2Driver=["UPDATE_COMPLETE", "t6"]))
        self.assertFalse(deploy.others_untouched(driver, redeployed, "sx3", "ColvennaCatalogTestU1")[0])
        # In another region too.
        there = {"stacks": {"ColvennaCatalogTestU1": ["UPDATE_COMPLETE", "t1"]},
                 "stacksElsewhere": {"ColvennaCatalogSu2Serverless": ["UPDATE_COMPLETE", "t2", "us-east-2"]},
                 "roles": ["colvenna-catalog-su2-purge"]}
        moved = dict(there, stacksElsewhere={"ColvennaCatalogSu2Serverless": ["UPDATE_COMPLETE", "t3", "us-east-2"]}, roles=[])
        self.assertTrue(deploy.others_untouched(there, moved, "sx3", "ColvennaCatalogTestU1")[0])
        # The case's own scripts naming that install's stacks: not excused.
        self.assertFalse(deploy.others_untouched(before, back, "sx3", "ColvennaCatalogTestU1",
                                                 said="updating ColvennaCatalogSu2Serverless")[0])

    def test_the_stacks_elsewhere(self):
        class FakeAws:
            def __init__(self, region, refused=()):
                self.region, self.refused, self.calls = region, refused, []

            def in_region(self, region):
                return FakeAws(region, self.refused)

            def __call__(self, *args):
                if args[:2] == ("ec2", "describe-regions"):
                    self.calls.append(args)
                    return ["us-west-2", "us-east-2", "eu-west-1"]
                if self.region in self.refused:
                    raise deploy.AwsError(list(args), 254, "AccessDenied")
                if self.region == "us-west-2":
                    return [{"StackName": "ColvennaCatalogEx2Eks", "StackStatus": "CREATE_COMPLETE", "CreationTime": "t",
                             "StackId": "arn:aws:cloudformation:us-west-2:111122223333:stack/ColvennaCatalogEx2Eks/"
                                        "33333333-3333-4333-8333-333333333333"}]
                return []

        here = FakeAws("us-east-2")
        self.assertEqual(deploy.stacks_elsewhere(here), {"ColvennaCatalogEx2Eks": [
            "CREATE_COMPLETE", "t", "33333333-3333-4333-8333-333333333333", "us-west-2"]})
        self.assertEqual(here.calls, [("ec2", "describe-regions", "--query", "Regions[].RegionName")])  # the enabled ones only
        # A region that refuses the call is left out.
        self.assertEqual(deploy.stacks_elsewhere(FakeAws("us-east-2", refused=("us-west-2",))), {})

    def test_the_data_of_an_install_on_a_restored_instance(self):
        """#51: after UAT-DEP-10 an install runs on the instance it restored, and the manual deletes the old one:
        its data is the bucket and that instance; the stack's own is wanted only if the account had it before."""
        storage = {"resources": {"B": ["AWS::S3::Bucket", "colvennacatalogek1storage-warehouse"],
                                 "D": ["AWS::RDS::DBInstance", "colvennacatalogek1storage-database"]}}
        after = {"buckets": ["colvennacatalogek1storage-warehouse"], "databases": []}
        restored = "colvenna-ek1-restored-u1"
        self.assertEqual(deploy.data_missing(storage, after), ["colvennacatalogek1storage-database"])
        self.assertEqual(deploy.data_missing(storage, after, restored=(restored, True), had=after), [])
        self.assertEqual(deploy.data_missing(storage, after, restored=(restored, False), had=after), [restored])
        self.assertEqual(deploy.data_missing(storage, {"buckets": [], "databases": []}, restored=(restored, True), had=after),
                         ["colvennacatalogek1storage-warehouse"])
        # The old instance still there before the uninstall (kept by UAT-DEP-07's branch, or --keep-old-instance):
        # it must still be there after.
        had = {"buckets": after["buckets"], "databases": ["colvennacatalogek1storage-database available"]}
        self.assertEqual(deploy.data_missing(storage, after, restored=(restored, True), had=had),
                         ["colvennacatalogek1storage-database"])
        self.assertEqual(deploy.data_missing(storage, had, restored=(restored, True), had=had), [])

    def test_the_previous_release_reached_without_strict_x509(self):
        from uatlib import lifecycle

        class Ctx:
            def __init__(self, form):
                self.form, self.probe = form, type("P", (), {"strict": True})()

        eks, serverless = Ctx("eks"), Ctx("serverless")
        lifecycle.reach_previous_release(eks)
        lifecycle.reach_previous_release(serverless)
        self.assertFalse(eks.probe.strict)
        self.assertTrue(serverless.probe.strict)  # a public certificate: strict as ever

    def test_the_inventories_differences_and_the_kept_data(self):
        self.assertEqual(deploy.differences({"buckets": ["a", "b"], "stacks": {"S": 1}}, {"buckets": ["a"], "stacks": {"S": 2}}),
                         ["buckets: gone ['b'], made []", "stacks S: 1 -> 2"])
        self.assertEqual(deploy.differences({"buckets": ["a"]}, {"buckets": ["a"]}), [])
        storage = {"resources": {"B": ["AWS::S3::Bucket", "colvennacatalogek1storage-b"],
                                 "D": ["AWS::RDS::DBInstance", "colvennacatalogek1storage-db"], "R": ["AWS::IAM::Role", "r"]}}
        there = {"buckets": ["colvennacatalogek1storage-b"], "databases": ["colvennacatalogek1storage-db available"]}
        self.assertEqual(deploy.data_missing(storage, there), [])
        self.assertEqual(deploy.data_missing(storage, dict(there, databases=[])), ["colvennacatalogek1storage-db"])
        self.assertTrue(deploy.judge_exit(run_of("x", 0))[0])
        self.assertFalse(deploy.judge_exit(run_of("== tools\n== stacks", 1))[0])

    def test_the_install_runs_the_candidate(self):
        release = {"source": {"built_from": "b" * 40}, "image": {"digest": "sha256:i"}, "chart": {"digest": "sha256:c"}}
        pod = {"colvenna-catalog/release": "v0.0.0-rc.9", "colvenna-catalog/source-sha": "b" * 40}
        state = {"repository": "colvenna-catalog-ek1/colvenna-catalog", "tagged": "sha256:i", "images": ["r@sha256:i"],
                 "annotations": {"p1": pod}, "chart": "sha256:c"}
        stacks = {"ColvennaCatalogEk1Eks": {"tags": {"SourceSha": "b" * 40}}}
        self.assertTrue(deploy.judge_runs_the_candidate(state, stacks, "v0.0.0-rc.9", release, "eks")[0])
        for bad in (dict(state, tagged=""), dict(state, images=["r@sha256:other"]), dict(state, chart="sha256:x"),
                    dict(state, annotations={"p1": dict(pod, **{"colvenna-catalog/release": "v0.0.0-rc.8"})})):
            self.assertFalse(deploy.judge_runs_the_candidate(bad, stacks, "v0.0.0-rc.9", release, "eks")[0], bad)
        self.assertFalse(deploy.judge_runs_the_candidate(state, {"S": {"tags": {}}}, "v0.0.0-rc.9", release, "eks")[0])
        serverless = {"repository": state["repository"], "tagged": "sha256:i", "images": ["r@sha256:i"]}
        self.assertTrue(deploy.judge_runs_the_candidate(serverless, stacks, "v0.0.0-rc.9", release, "serverless")[0])

    def test_a_configuration_changed_in_some_keys(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "a.env"
            base.write_text("# c\nCOLVENNA_ENV=ek1\nCOLVENNA_DB=create\nCOLVENNA_BUCKET=\n")
            out = deploy.write_config(Path(d) / "b.env", base, COLVENNA_DB="existing", COLVENNA_BUCKET=None,
                                      COLVENNA_DB_ENDPOINT="db.example.com")
            self.assertEqual(out.read_text(), "# c\nCOLVENNA_ENV=ek1\nCOLVENNA_DB=existing\nCOLVENNA_DB_ENDPOINT=db.example.com\n")


class Cases(unittest.TestCase):
    def test_a_refusal_is_its_status_and_its_message(self):
        r = run_of("== tools\n== preflight\npreflight: No EKS cluster x in us-west-2 that these credentials can read", 3)
        self.assertTrue(dep05.refused(r, 3, ("No EKS cluster",))[0])
        self.assertFalse(dep05.refused(r, 2, ("No EKS cluster",))[0])
        self.assertFalse(dep05.refused(r, 3, ("reader endpoint",))[0])

    def test_dep05_lists_the_forms_refusals(self):
        self.assertIn("eks-db-reader-endpoint", dep05.checks("eks"))
        self.assertNotIn("serverless-one-zone", dep05.checks("eks"))
        self.assertIn("serverless-db-without-iam-auth", dep05.checks("serverless"))
        self.assertNotIn("flag-overrides-refusal", dep05.checks("serverless"))

    def test_uninstall_removes_and_keeps_by_form(self):
        before = {"ColvennaCatalogEk1Storage": {"status": "CREATE_COMPLETE", "resources": {}},
                  "ColvennaCatalogEk1Eks": {"status": "CREATE_COMPLETE", "resources": {}}}
        after = {"ColvennaCatalogEk1Storage": {"status": "CREATE_COMPLETE", "resources": {}}}
        kube = {"secrets": {"colvenna-catalog-ca-key": "1"}, "helm": None, "namespace": True}
        (removed, _), (kept, _) = dep08.judge_removed_kept("eks", before, after, "ek1", kube)
        self.assertTrue(removed and kept)
        self.assertFalse(dep08.judge_removed_kept("eks", before, after, "ek1", kube, {"colvenna-catalog-nlb": True})[0][0])
        self.assertTrue(dep08.judge_removed_kept("eks", before, after, "ek1", kube, {"colvenna-catalog-nlb": False})[0][0])
        self.assertFalse(dep08.judge_removed_kept("eks", before, after, "ek1", kube, data_missing=["db"])[1][0])
        self.assertTrue(removed and kept)
        kube_left = dict(kube, secrets={"colvenna-catalog-ca-key": "1", "colvenna-catalog-admin-token": "2"})
        self.assertFalse(dep08.judge_removed_kept("eks", before, after, "ek1", kube_left)[0][0])
        db = {"Db": ["AWS::RDS::DBInstance", "x"]}
        sl_before = {"ColvennaCatalogSl1Storage": {"status": "CREATE_COMPLETE", "resources": db},
                     "ColvennaCatalogSl1Network": {"status": "CREATE_COMPLETE", "resources": {}},
                     "ColvennaCatalogSl1Images": {"status": "CREATE_COMPLETE", "resources": {}},
                     "ColvennaCatalogSl1Serverless": {"status": "CREATE_COMPLETE", "resources": {}}}
        sl_after = {k: v for k, v in sl_before.items() if k.endswith(("Storage", "Network"))}
        (removed, _), (kept, d) = dep08.judge_removed_kept("serverless", sl_before, sl_after, "sl1")
        self.assertTrue(removed and kept, d)

    def test_the_reinstalls_configuration(self):
        class Install:
            def __init__(self, form, **config):
                self.form, self.config = form, config

            def get(self, k):
                return self.config.get(k, "")

            def stack(self, part):
                return "ColvennaCatalogEk1" + part

        outputs = {"BucketName": "b", "DbEndpoint": "e", "DbPort": "5432", "DbAdminSecretArn": "<ARN>"}
        stacks = {"ColvennaCatalogEk1Storage": {"outputs": outputs}}
        self.assertEqual(dep08.reinstall_config(Install("eks", COLVENNA_DB="create"), stacks),
                         {"COLVENNA_DB": "existing", "COLVENNA_BUCKET": "b", "COLVENNA_DB_ENDPOINT": "e",
                          "COLVENNA_DB_PORT": "5432", "COLVENNA_DB_ADMIN_SECRET_ARN": "<ARN>"})
        self.assertEqual(dep08.reinstall_config(Install("serverless", COLVENNA_NETWORK="create"), stacks), {})

    def test_what_was_given_and_not_made(self):
        class Install:
            env = "ex1"

            def __init__(self, **config):
                self.config = config

            def get(self, k):
                return self.config.get(k, "")

            def outputs(self, part):
                return {"DbEndpoint": "colvennacatalogex1storage-db.abc.us-west-2.rds.amazonaws.com"} if part == "Storage" else {}

        given = Install(COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT="uatdb.abc.us-west-2.rds.amazonaws.com",
                        COLVENNA_BUCKET="test-env-bucket")
        self.assertEqual(dep09.existing(given), ("uatdb", "test-env-bucket"))
        self.assertIsNone(dep09.restored(given))
        kept = Install(COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT="colvennacatalogex1storage-db.abc.rds.amazonaws.com",
                       COLVENNA_BUCKET="colvennacatalogex1storage-warehouse")
        self.assertEqual(dep09.existing(kept), (None, None))
        # E1 after UAT-DEP-10: pointed at the instance restored, with the bucket it made.
        moved = Install(COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT="restored-ex1.abc.us-west-2.rds.amazonaws.com",
                        COLVENNA_BUCKET="colvennacatalogex1storage-warehouse")
        self.assertEqual(dep09.restored(moved), "restored-ex1")
        self.assertEqual(dep09.existing(moved), (None, None))
        self.assertIsNone(dep09.restored(Install(COLVENNA_DB="create")))
        # UAT-DEP-08's install onto the kept data names the Storage stack's own instance: not a restored one.
        self.assertIsNone(dep09.restored(kept))

    def test_a_rerun_changes_nothing(self):
        record = {"stacks": {"S": {"resources": {"A": ["T", "a"]}}}, "repositories": {"r": ["d t"]}, "adminToken": "7",
                  "kube": {"pods": [{"name": "p1", "uid": "u1"}, {"name": "p2", "uid": "u2"}], "helm": {"revision": 3},
                           "values": {"x": 1}},
                  "principals": [["admin", None]], "grants": [1], "tables": {"default": []}}
        results = dep03.judge("eks", False, run_of(EKS_OUTPUT), record, record)
        self.assertTrue(all(ok for ok, _ in results.values()), results)
        restarted = dict(record, kube=dict(record["kube"], pods=[{"name": "p3", "uid": "u3"}, {"name": "p4", "uid": "u4"}]))
        self.assertFalse(dep03.judge("eks", False, run_of(EKS_OUTPUT), record, restarted)["no-restart"][0])
        # Fewer pods than the Deployment asks for: one was being replaced.
        short = dict(record, kube=dict(record["kube"], replicas=3))
        self.assertFalse(dep03.judge("eks", False, run_of(EKS_OUTPUT), short, short)["no-restart"][0])


class Runner(unittest.TestCase):
    """deploy.Installer against a stand-in install.sh."""

    SCRIPT = textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "== tools"
        echo "== preflight"
        echo "  account   <ACCOUNT_ID>"
        if [[ " $* " != *" --yes "* ]]; then
          [[ -t 0 ]] || { echo "Not confirmed: no terminal to ask on; rerun with --yes" >&2; exit 2; }
          read -r -p "Install? Type the environment name (ek1) to go on: " answer
          [[ "$answer" == ek1 ]] || { echo "Not confirmed" >&2; exit 2; }
        fi
        echo "== stacks"
        echo "== chart"
        sleep 30
        echo "== admin"
        """)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "install.sh").write_text(self.SCRIPT)
        (self.dir / "install.sh").chmod(0o755)
        (self.dir / "c.env").write_text("COLVENNA_ENV=ek1\n")
        self.kept = {}

        class Ctx:
            class aws:
                profile = "test"

            @staticmethod
            def keep(name, text):
                self.kept[name] = text

        self.installer = deploy.Installer(Ctx, self.dir, self.dir / "c.env")

    def test_no_terminal_no_confirmation(self):
        r = self.installer.run("install.sh", timeout=30)
        self.assertEqual(r.exit_code, 2)
        self.assertIn("Not confirmed: no terminal", r.text)

    def test_the_confirmation_typed_on_a_terminal(self):
        r = self.installer.run("install.sh", answer="ek1", signal_when=lambda lines: any(t == "== chart" for _, t in lines),
                               sig=signal.SIGKILL, timeout=60)
        self.assertEqual(r.steps()[:4], ["tools", "preflight", "stacks", "chart"])
        self.assertIn("SIGKILL in step chart", r.signalled)
        self.assertNotEqual(r.exit_code, 0)
        self.assertLess(r.index(r"Type the environment name"), r.index(r"^== stacks$"))

    def test_signalled_in_its_step(self):
        r = self.installer.run("install.sh", "--yes", signal_when=lambda lines: any(t == "== chart" for _, t in lines),
                               sig=signal.SIGHUP, timeout=60)
        self.assertEqual(r.steps()[-1], "chart")
        self.assertIn("SIGHUP", r.signalled)
        self.assertIn(r.exit_code, (-signal.SIGHUP, 128 + signal.SIGHUP))

    def test_signalled_in_its_step_when_the_harness_inherited_sighup_ignored(self):
        # As under nohup (#76): the harness ignores SIGHUP; the script it starts still gets it.
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            self.test_signalled_in_its_step()
        finally:
            signal.signal(signal.SIGHUP, previous)


class Dep01And02(unittest.TestCase):
    from cases import dep01 as d1, dep02 as d2

    def test_release_checks_and_preflight(self):
        notes = ("checkout: abc, clean, differs from b (built_from) under release/x/ alone\nform: eks is among\n"
                 "registry: r@d is there\nregistry: c@d is there\nimage: r@d is labelled x\nchart: c is x, annotated y\n")
        chart = "chart: oci://<registry>/charts/colvenna-catalog@sha256:c"
        good = run_of(f"== tools\n== preflight\n{chart}\nColvenna Catalog install\n== stacks")
        self.assertTrue(self.d1.judge_release_checks_first(notes, good, "sha256:c")[0])
        self.assertFalse(self.d1.judge_release_checks_first(notes, run_of(f"== tools\n== preflight\n{chart}"), "sha256:c")[0])  # preflight did not end
        self.assertFalse(self.d1.judge_release_checks_first(notes.replace("form: eks is among\n", ""), good, "sha256:c")[0])
        later = run_of(f"== tools\n== preflight\npreflight: a cluster check\n{chart}\n== stacks")
        self.assertFalse(self.d1.judge_release_checks_first(notes, later, "sha256:c")[0])
        self.assertFalse(self.d1.judge_release_checks_first(notes, good, "sha256:other")[0])

    def test_nothing_built(self):
        copy = {"image.log": "2026/10/29 Copying from <registry>/r@sha256:x to <repo>:v0", "cdk.log": "deploying"}
        self.assertTrue(self.d1.judge_nothing_built(copy, ["== image"])[0])
        self.assertFalse(self.d1.judge_nothing_built({"mvn.log": "[INFO] BUILD SUCCESS"}, [])[0])
        self.assertFalse(self.d1.judge_nothing_built({"image.log": "#1 [internal] load build definition"}, [])[0])
        self.assertFalse(self.d1.judge_nothing_built(copy, ["[INFO] BUILD SUCCESS"])[0])

    def test_admin_token(self):
        token = "colvenna_" + "B" * 43
        ok = self.d1.judge_admin_token_not_printed({"output": "fine"}, token, {"status": 200}, "x")
        self.assertTrue(ok[0])
        self.assertFalse(self.d1.judge_admin_token_not_printed({"output": token}, token, {"status": 200}, "x")[0])
        self.assertFalse(self.d1.judge_admin_token_not_printed({"output": "fine"}, token, {"status": 401}, "x")[0])

    def test_the_cycle(self):
        good = {"exitCode": 0, "result": {"eventsMatch": True, "keyedMatch": True, "dataTokenEnabled": True,
                                          "perTableCredentials": {"tokenStatus": 200, "s3": {"getInTableRoot": "allowed",
                                                                                             "putInOtherTable": "AccessDenied"}}}}
        self.assertTrue(self.d1.judge_cycle(good)[0])
        leaky = {"exitCode": 0, "result": dict(good["result"], perTableCredentials={"tokenStatus": 200, "s3": {
            "getInTableRoot": "allowed", "putInOtherTable": "allowed"}})}
        self.assertFalse(self.d1.judge_cycle(leaky)[0])
        self.assertFalse(self.d1.judge_cycle({"exitCode": 1, "result": None})[0])
        self.assertTrue(self.d1.judge_run_sh_cycle(0, "cycle: PASS\n")[0])
        self.assertFalse(self.d1.judge_run_sh_cycle(1, "")[0])

    def test_the_installs_alongside_by_their_configurations(self):
        class Inst:
            env, prefix = "ex1", "ex1"
            config = {"COLVENNA_DB": "existing", "COLVENNA_DB_ENDPOINT": "db.example.com", "COLVENNA_BUCKET": "bucket",
                      "COLVENNA_DB_NAME": "ex1_catalog", "COLVENNA_DB_USER": "ex1_catalog"}

            def get(self, key):
                return self.config.get(key, "")

        work = Path(tempfile.mkdtemp())

        def config(name, **values):
            path = work / f"{name}.env"
            settings = dict(COLVENNA_ENV=name, COLVENNA_DB="existing", COLVENNA_DB_ENDPOINT="db.example.com", COLVENNA_BUCKET="bucket",
                            COLVENNA_DB_NAME=f"{name}_catalog", COLVENNA_DB_USER=f"{name}_catalog", COLVENNA_WAREHOUSE_PREFIX=name)
            path.write_text("".join(f"{k}={v}\n" for k, v in dict(settings, **values).items()))
            return str(path)

        names = self.d2.alongside_names(Inst(), [config("sx1")])
        self.assertEqual(names, {"databases": {"sx1_catalog"}, "roles": {"sx1_catalog", "sx1_catalog_authz"}, "prefixes": {"sx1/"}})
        self.assertEqual(self.d2.alongside_names(Inst(), []), {"databases": set(), "roles": set(), "prefixes": set()})
        # Sharing the bucket only, its prefix there is its; sharing the instance only, its database and users.
        self.assertEqual(self.d2.alongside_names(Inst(), [config("sb1", COLVENNA_DB="create", COLVENNA_DB_ENDPOINT="")]),
                         {"databases": set(), "roles": set(), "prefixes": {"sb1/"}})
        self.assertEqual(self.d2.alongside_names(Inst(), [config("sd1", COLVENNA_BUCKET="")]),
                         {"databases": {"sd1_catalog"}, "roles": {"sd1_catalog", "sd1_catalog_authz"}, "prefixes": set()})
        for bad, needle in ((config("ex1"), "that is this install's configuration"),
                            (config("sx2", COLVENNA_DB_ENDPOINT="other.example.com", COLVENNA_BUCKET="other"),
                             "installs into neither this install's existing database nor its bucket"),
                            (config("sx3", COLVENNA_DB_USER="ex1_catalog"), "this install's names ['ex1_catalog', 'ex1_catalog_authz']")):
            with self.assertRaises(ValueError) as raised:
                self.d2.alongside_names(Inst(), [bad])
            self.assertIn(needle, str(raised.exception))

    def test_existing_untouched_and_fifteen_minutes(self):
        own = {"databases": {"cat"}, "roles": {"cat", "cat_authz"}, "prefixes": {"wh/"}, "stacks": {"ColvennaCatalogEx1Eks"}}
        made = {"databases": ["cat"], "roles": ["cat"], "prefixes": ["wh/"], "stacks": ["ColvennaCatalogEx1Eks"]}
        self.assertTrue(self.d2.judge_existing_untouched({"vpc": 1, "database": 2}, {"vpc": 1, "database": 2}, made, own)[0])
        self.assertFalse(self.d2.judge_existing_untouched({"vpc": 1, "database": 2}, {"vpc": 1, "database": 3}, made, own)[0])
        self.assertFalse(self.d2.judge_existing_untouched({"vpc": 1}, {"vpc": 1}, dict(made, roles=["cat", "other"]), own)[0])
        self.assertFalse(self.d2.judge_existing_untouched({"vpc": 1}, {"vpc": 1}, dict(made, stacks=["ColvennaCatalogX"]), own)[0])
        # #43: another install into the same instance at the same time (E3 and S3 in one step) adds its own; named
        # with --alongside, they are its, not this install's; anything else added still is a problem.
        both = {"databases": ["cat", "sx_catalog"], "roles": ["cat", "sx_catalog", "sx_catalog_authz"], "prefixes": ["sx/", "wh/"],
                "stacks": ["ColvennaCatalogEx1Eks"]}
        alongside = {"databases": {"sx_catalog"}, "roles": {"sx_catalog", "sx_catalog_authz"}, "prefixes": {"sx/"}}
        ok, detail = self.d2.judge_existing_untouched({"vpc": 1}, {"vpc": 1}, both, own)
        self.assertFalse(ok, "without --alongside, as before")
        self.assertIn("name it with --alongside", detail)
        ok, detail = self.d2.judge_existing_untouched({"vpc": 1}, {"vpc": 1}, both, own, alongside)
        self.assertTrue(ok, detail)
        self.assertIn("added by the installs alongside: {'databases': ['sx_catalog']", detail)
        ok, detail = self.d2.judge_existing_untouched({"vpc": 1}, {"vpc": 1}, dict(both, roles=both["roles"] + ["colvenna_catalog"]),
                                                      own, alongside)
        self.assertFalse(ok)
        self.assertIn("not the install's: {'roles': ['colvenna_catalog']}", detail)
        # The instance itself changed: alongside or not, touched.
        self.assertFalse(self.d2.judge_existing_untouched({"database": 2}, {"database": 3}, both, own, alongside)[0])
        self.assertEqual(self.d2.deployed_stacks({"cdk.log": "ColvennaCatalogEx1Eks: deploying... ColvennaCatalogEx1Storage/B"}),
                         ["ColvennaCatalogEx1Eks", "ColvennaCatalogEx1Storage"])
        self.assertTrue(self.d2.judge_ready_within("2026-10-29T00:00:00Z", "2026-10-29T00:14:59Z", True)[0])
        self.assertFalse(self.d2.judge_ready_within("2026-10-29T00:00:00Z", "2026-10-29T00:15:01Z", True)[0])
        self.assertFalse(self.d2.judge_ready_within("2026-10-29T00:00:00Z", "2026-10-29T00:05:00Z", False)[0])


class Dep04(unittest.TestCase):
    from cases import dep04 as d

    def test_an_interruption_in_its_step(self):
        r = run_of("== tools\n== preflight\n== stacks\n== image\n== namespace\n== database\n== tls\n== chart", -1)
        r.signalled = "SIGHUP in step chart at t"
        self.assertTrue(self.d.judge_interrupted(r, "eks", "pending-upgrade")[0])
        late = run_of(EKS_OUTPUT, 0)
        self.assertFalse(self.d.judge_interrupted(late, "eks", "deployed")[0])  # never signalled: no interruption
        r.signalled = ""
        self.assertFalse(self.d.judge_interrupted(r, "eks", "deployed")[0])

    def test_resumed_and_converged(self):
        self.assertTrue(self.d.judge_resumed(run_of(EKS_OUTPUT), "eks", False, "deployed")[0])
        self.assertFalse(self.d.judge_resumed(run_of(EKS_OUTPUT), "eks", False, "pending-upgrade")[0])
        self.assertTrue(self.d.judge_converged(7190, 7200, [], (True, "2 pods"), True)[0])
        self.assertFalse(self.d.judge_converged(10790, 7200, [], (True, "2 pods"), True)[0])
        self.assertFalse(self.d.judge_converged(7190, 7200, ["S: 2 AWS::Lambda::Version (was 1)"], (True, "2 pods"), True)[0])
        version = {"S": {"resources": {"V1": ["AWS::Lambda::Version", "v1"], "F": ["AWS::Lambda::Function", "f"]}}}
        replaced = {"S": {"resources": {"V2": ["AWS::Lambda::Version", "v2"], "F": ["AWS::Lambda::Function", "f"]}}}
        self.assertEqual(deploy.duplicates(version, replaced), [])
        doubled = {"S": {"resources": dict(replaced["S"]["resources"], V1=["AWS::Lambda::Version", "v1"])}, "N": {"resources": {}}}
        self.assertEqual(deploy.duplicates(version, doubled), ["N: made", "S: 2 AWS::Lambda::Version (was 1)"])
        self.assertEqual(self.d.duration_of("PT1H30M"), 5400)


class Dep05And08And09And11(unittest.TestCase):
    from cases import dep05 as d5, dep08 as d8, dep09 as d9, dep11 as d11

    def test_nothing_made(self):
        self.assertTrue(self.d5.judge_nothing_made({}, ["UPDATE_COMPLETE", "t"], ["UPDATE_COMPLETE", "t"], True)[0])
        ok, detail = self.d5.judge_nothing_made({}, None, None, None)
        self.assertTrue(ok)
        self.assertIn("not this form's run", detail)
        self.assertFalse(self.d5.judge_nothing_made({}, None, None, False)[0])

    def test_the_flag_and_enforcement_back(self):
        self.assertTrue(self.d5.judge_no_config(2, "install.sh: --config is required\n")[0])
        self.assertFalse(self.d5.judge_no_config(1, "install.sh: --config is required\n")[0])
        record = {"stacks": {"S": {"resources": {"A": ["T", "a"]}}}, "repositories": {"r": ["d t"]}, "adminToken": "7",
                  "kube": {"pods": [{"name": "p1", "uid": "u1"}], "replicas": 1, "helm": {"revision": 3}, "values": {"x": 1}},
                  "principals": [["admin", None]], "grants": [1], "tables": {"default": []}}
        self.assertTrue(self.d5.judge_flag_overrides(run_of(EKS_OUTPUT), record, record)[0])
        refused = run_of("== tools\n== preflight\npreflight: NetworkPolicy is not enforced", 3)
        self.assertFalse(self.d5.judge_flag_overrides(refused, record, record)[0])
        moved = dict(record, kube=dict(record["kube"], values={"x": 2}))
        self.assertFalse(self.d5.judge_flag_overrides(run_of(EKS_OUTPUT), record, moved)[0])
        e1 = self.d5.deployment(types.SimpleNamespace(namespace="colvenna-catalog", release="colvenna-catalog"))
        restarted = "Started the catalog pods of colvenna-catalog/colvenna-catalog again\n"
        self.assertTrue(self.d5.judge_restarted(restarted, e1)[0])
        self.assertFalse(self.d5.judge_restarted("Started the catalog pods of other/other again\n", e1)[0])
        enforced = "fx4: 2 catalog and 2 clients nodes Ready, NetworkPolicy enforced (a new client let through after 9s)\n"
        self.assertTrue(self.d5.judge_enforcement_back(0, restarted + enforced, e1)[0])
        self.assertFalse(self.d5.judge_enforcement_back(0, enforced, e1)[0])
        self.assertFalse(self.d5.judge_enforcement_back(1, restarted + "A client the NetworkPolicy admits ... not let through\n", e1)[0])
        self.assertFalse(self.d5.judge_nothing_made({"stacks": {"ColvennaCatalogRf1Eks": 1}}, None, None, True)[0])
        self.assertFalse(self.d5.judge_nothing_made({}, ["UPDATE_COMPLETE", "t"], ["UPDATE_COMPLETE", "u"], True)[0])

    def test_the_aurora_fixture_is_wrong_in_iam_authentication_only(self):
        # #29: it requires TLS (rds.force_ssl=1 in a cluster parameter group of its own), which install.sh
        # checks before IAM database authentication, so its refusal is the one the check is about.
        calls, cleanups = [], []

        def aws(*args, **_):
            calls.append(args)
            if args[1] == "describe-db-engine-versions":
                return ["16.4", "16.6"]
            if args[1] == "describe-db-clusters":
                return {"DBClusters": [{"Status": "available", "Endpoint": "w", "ReaderEndpoint": "r", "Port": 5432}]}
            return {}

        ctx = types.SimpleNamespace(aws=aws, testenv=types.SimpleNamespace(output=lambda k: "subnet-a,subnet-b"),
                                    on_cleanup=lambda what, fn: cleanups.append(what))
        self.assertEqual(self.d5.Fixtures(ctx, "uat-r1").aurora(), ("w", "r", 5432))
        made = {a[1]: a for a in calls}
        self.assertEqual(made["create-db-cluster-parameter-group"][made["create-db-cluster-parameter-group"].index(
            "--db-parameter-group-family") + 1], "aurora-postgresql16")
        self.assertIn("ParameterName=rds.force_ssl,ParameterValue=1,ApplyMethod=pending-reboot",
                      made["modify-db-cluster-parameter-group"])
        cluster = made["create-db-cluster"]
        self.assertEqual(cluster[cluster.index("--db-cluster-parameter-group-name") + 1], "uat-r1-aurora")
        self.assertIn("--no-enable-iam-database-authentication", cluster)
        order = [a[1] for a in calls if a[1].startswith("create") or a[1].startswith("modify")]
        self.assertEqual(order, ["create-db-subnet-group", "create-db-cluster-parameter-group",
                                 "modify-db-cluster-parameter-group", "create-db-cluster"])
        # Clean-ups run last registered first: the cluster, then its parameter group, then the subnet group.
        self.assertEqual(cleanups, ["DB subnet group uat-r1-aurora deleted", "DB cluster parameter group uat-r1-aurora deleted",
                                    "Aurora cluster uat-r1-aurora deleted"])

    def test_the_endpoint_of_another_vpc_is_s1s_own(self):
        available = {"vpce-s1": "vpc-s1", "vpce-a": "vpc-other", "vpce-test": "vpc-test"}
        self.assertEqual(self.d5.choose_foreign_endpoint(available, "vpc-test", "vpce-s1"), "vpce-s1")
        # Without S1's own endpoint (an existing network, or not available), none: never another of the
        # Region, which may be another repository's, nor the test environment's.
        for own in (None, "vpce-test", "vpce-gone"):
            with self.assertRaisesRegex(RuntimeError, "UAT-DEP-01"):
                self.d5.choose_foreign_endpoint(available, "vpc-test", own)

    def test_the_second_uninstall_and_the_reinstall(self):
        self.assertTrue(self.d8.judge_uninstall_again(run_of("x", 0), [])[0])
        self.assertFalse(self.d8.judge_uninstall_again(run_of("x", 0), ["buckets: gone ['b']"])[0])
        tables = {"events": {"rows": 150, "sha256": "x", "snapshots": [1]}}
        self.assertTrue(self.d8.judge_reinstall(run_of("x", 0), tables, tables, {"status": 200}, True, True)[0])
        self.assertFalse(self.d8.judge_reinstall(run_of("x", 0), tables, {"events": {"rows": 0}}, {"status": 200}, True, True)[0])
        self.assertFalse(self.d8.judge_reinstall(run_of("x", 0), tables, tables, {"status": 401}, True, True)[0])
        keyed = {"keyed": {"rows": 150, "sha256": "y", "snapshots": [1, 2]}}
        listed = {"keyed": {"rows": 150, "sha256": "y", "snapshots": [2, 1]}}
        self.assertTrue(self.d8.judge_reinstall(run_of("x", 0), keyed, listed, {"status": 200}, True, True)[0])
        self.assertFalse(self.d8.judge_reinstall(run_of("x", 0), keyed, {"keyed": dict(keyed["keyed"], snapshots=[1])},
                                                 {"status": 200}, True, True)[0])

    def test_uninstall_deleting_the_data(self):
        there = run_of("== tools\nuninstall.sh: --delete-data: restored-ek1, which the install did not make, uses ...", 3)
        self.assertTrue(self.d9.judge_restored_refused(there, "restored-ek1", True)[0])
        self.assertFalse(self.d9.judge_restored_refused(there, "restored-ek1", False)[0])
        asked = run_of("== tools\nrestored-ek1 ...\nNot confirmed: nothing was deleted", 3)
        self.assertFalse(self.d9.judge_restored_refused(asked, "restored-ek1", True)[0])  # the name was asked for first
        self.assertFalse(self.d9.judge_restored_refused(run_of("== tools\nNot confirmed", 2), "restored-ek1", True)[0])
        self.assertTrue(self.d9.judge_uninstall_exit(run_of("x", 0))[0])
        ok, detail = self.d9.judge_uninstall_exit(run_of("x", 1), run_of("x", 0))
        self.assertFalse(ok)
        self.assertIn("run again", detail)
        r = run_of("== tools\n--delete-data deletes ...\nNot confirmed: nothing was deleted", 2)
        self.assertTrue(self.d9.judge_needs_the_name(r, True)[0])
        self.assertFalse(self.d9.judge_needs_the_name(r, False)[0])
        self.assertTrue(self.d9.judge_nothing_left(0, "ok    stacks: none\nok    buckets: none", [])[0])
        self.assertFalse(self.d9.judge_nothing_left(1, "LEFT  buckets: b", [])[0])
        self.assertFalse(self.d9.judge_nothing_left(0, "ok    stacks: none", ["the Helm release"])[0])
        marker = self.d9.marker_key("exw2", "u261030")
        kept = {marker: '"e0"'}
        self.assertTrue(self.d9.judge_existing_kept(True, True, kept, dict(kept), marker)[0])
        self.assertFalse(self.d9.judge_existing_kept(True, False, kept, dict(kept), marker)[0])  # the database gone
        self.assertFalse(self.d9.judge_existing_kept(False, True, kept, dict(kept), marker)[0])  # the instance changed
        ok, detail = self.d9.judge_existing_kept(True, True, None, None, None)
        self.assertTrue(ok)
        self.assertIn("no bucket was given", detail)

    def test_the_prefix_kept(self):
        """#44: the cases before UAT-DEP-09 drop their tables, whose files the catalog deletes, so the case puts a
        marker of its own under the prefix: the prefix is kept when the marker is still there, unchanged."""
        marker = self.d9.marker_key("exw2", "u261030")
        self.assertEqual(marker, "exw2/uat-dep09-u261030.marker")
        judge = self.d9.judge_existing_kept
        # The prefix held nothing but the marker.
        self.assertTrue(judge(True, True, {marker: '"e0"'}, {marker: '"e0"'}, marker)[0])
        ok, detail = judge(True, True, {marker: '"e0"'}, {}, marker)
        self.assertFalse(ok)
        self.assertIn("the marker gone or changed", detail)
        self.assertFalse(judge(True, True, {marker: '"e0"'}, {marker: '"e1"'}, marker)[0])
        # A dropped table's files the catalog was still deleting: reported, not a loss.
        before = {marker: '"e0"', "exw2/db.db/t/snapshot/snapshot-1": '"a1"'}
        ok, detail = judge(True, True, before, {marker: '"e0"'}, marker)
        self.assertTrue(ok)
        self.assertIn("of the 1 other objects recorded before the uninstall, 1 gone since", detail)
        self.assertTrue(judge(True, True, before, dict(before, **{"exw2/new": '"n"'}), marker)[0])
        # Without the marker recorded the check cannot see a prefix kept.
        self.assertFalse(judge(True, True, {}, {marker: '"e0"'}, marker)[0])

    def test_the_prefix_objects(self):
        class Aws:
            def __init__(self, out):
                self.out, self.calls = out, []

            def __call__(self, *args):
                self.calls.append(args)
                return self.out

        aws = Aws({"Contents": [{"Key": "exw2/a", "ETag": '"1"'}, {"Key": "exw2/b", "ETag": '"2"'}]})
        self.assertEqual(self.d9.prefix_objects(aws, "bucket", "exw2"), {"exw2/a": '"1"', "exw2/b": '"2"'})
        self.assertEqual(aws.calls, [("s3api", "list-objects-v2", "--bucket", "bucket", "--prefix", "exw2/")])
        self.assertEqual(self.d9.prefix_objects(Aws(None), "bucket", "exw2"), {})  # an empty prefix: no output
        self.assertEqual(self.d9.prefix_objects(Aws({"RequestCharged": None}), "bucket", "exw2"), {})

    def test_the_internal_nlb(self):
        internal = {"Scheme": "internal", "Type": "network", "AvailabilityZones": [{"LoadBalancerAddresses": []}]}
        self.assertTrue(self.d11.judge_nlb_internal(internal)[0])
        self.assertFalse(self.d11.judge_nlb_internal(dict(internal, Scheme="internet-facing"))[0])
        self.assertFalse(self.d11.judge_nlb_internal(None)[0])
        cycle = {"eventsMatch": True, "keyedMatch": True, "dataTokenEnabled": True}
        self.assertTrue(self.d11.judge_nlb_cycle(cycle, {"verified": True, "names": ["nlb.example.com"]}, "nlb.example.com")[0])
        self.assertFalse(self.d11.judge_nlb_cycle(cycle, {"verified": True, "names": ["svc"]}, "nlb.example.com")[0])
        self.assertTrue(self.d11.judge_refused({"a": {"status": 401}, "b": {"status": 401}}, 2)[0])
        self.assertFalse(self.d11.judge_refused({"a": {"status": 401}, "b": {"status": 200}}, 2)[0])
        self.assertFalse(self.d11.judge_refused({"a": {"status": 401}}, 2)[0])
        self.assertTrue(self.d11.judge_only_allowed({"connect": "timeout"}, "no connection")[0])
        self.assertFalse(self.d11.judge_only_allowed({"connect": "connected"}, "no connection")[0])
        self.assertTrue(self.d11.judge_nlb_removed(run_of("x", 0), "arn:x", ["tg"], [])[0])
        self.assertFalse(self.d11.judge_nlb_removed(run_of("x", 0), "arn:x", ["tg"], ["tg"])[0])


class Dep09DropsWhatWasGiven(unittest.TestCase):
    """UAT-DEP-09 drops the catalog's database and users from an existing instance with the deployment
    guide's statements, which uninstall.sh --delete-data prints (#73)."""

    PRINTED = """release: removed; ColvennaCatalogEx1Eks: removed
What the install was given stays. Remove the catalog's part of it if you no longer need it (deployment guide, section 8):
  as the admin user of db.example.com (if it is still there), connected to the database postgres:
    GRANT "ex1_user" TO CURRENT_USER;
    DROP DATABASE "ex1_db";
    DROP ROLE "ex1_user";
  aws s3 rm s3://bucket/ex1/ --recursive
"""

    def test_the_guide_s_statements_and_what_uninstall_printed(self):
        self.assertEqual(dep09.printed_statements(self.PRINTED), dep09.guide_statements("eks", "ex1_db", "ex1_user"))
        self.assertEqual(dep09.guide_statements("serverless", "d", "u")[-2:], ['DROP ROLE "u";', 'DROP ROLE "u_authz";'])
        self.assertEqual(dep09.printed_statements("release: removed\n"), [])

    def test_what_uninstall_sh_prints_is_read(self):
        repo = Path(__file__).resolve().parents[2]
        script = (f'source "{repo}/uninstall.sh"; COLVENNA_DB_ENDPOINT=db.example.com COLVENNA_DB_NAME=ex1_db '
                  'COLVENNA_DB_USER=ex1_user COLVENNA_FORM=serverless COLVENNA_BUCKET=; print_given_leftovers 1')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(dep09.printed_statements(out), dep09.guide_statements("serverless", "ex1_db", "ex1_user"))

    def test_the_guide_says_the_same(self):
        guide = (Path(__file__).resolve().parents[2] / "docs/manual/install.md").read_text()
        section = guide.split("## 8. Uninstalling", 1)[1].split("\n## ", 1)[0]
        block = section.split("```sql\n", 1)[1].split("```", 1)[0]
        statements = [line.split("--", 1)[0].strip() for line in block.splitlines()]
        self.assertEqual(statements, dep09.guide_statements("serverless", "colvenna_catalog", "colvenna_catalog"))


if __name__ == "__main__":
    unittest.main()
