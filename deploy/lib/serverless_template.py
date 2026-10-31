#!/usr/bin/env python3
"""The serverless stack's deployed template, as install.sh changes it before an upgrade's migrate
(deploy/lib/serverless.sh, sl_maintenance_first; #63): the maintenance function as this version
makes it, and nothing else changed.

usage:
  serverless_template.py maintenance-first <deployed template> <synthesized template> <function name>
      The deployed template (as `aws cloudformation get-template --query TemplateBody --output json`
      prints it: the template, or a string holding it), with the one AWS::Lambda::Function named
      <function name> given the properties the synthesized template (this version's, `cdk synth`)
      gives it: its image, settings and database among them; the policy of its role given the
      synthesized one's document (the database it may sign in to); and the output MaintenanceImage,
      if both have it, the synthesized one's value. Prints the changed template as compact JSON. Exit 1,
      naming why, when either template has no such function or more than one, its role has more than
      one policy, or what the new properties refer to is not in the deployed template.
"""
import copy
import json
import sys

# What a template may refer to without a resource of its own.
PSEUDO = {"AWS::AccountId", "AWS::NotificationARNs", "AWS::NoValue", "AWS::Partition", "AWS::Region", "AWS::StackId",
          "AWS::StackName", "AWS::URLSuffix"}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            template = json.load(f)
        return json.loads(template) if isinstance(template, str) else template
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: not a template ({e})") from e


def named_function(template, name, which):
    found = [rid for rid, r in template.get("Resources", {}).items()
             if r.get("Type") == "AWS::Lambda::Function" and r.get("Properties", {}).get("FunctionName") == name]
    if len(found) != 1:
        raise ValueError(f"the {which} template has {len(found)} functions named {name}, not one")
    return found[0]


def attribute_of(value):
    """The logical ID an Fn::GetAtt names, in either of its forms ([id, attribute], or "id.attribute")."""
    target = value["Fn::GetAtt"]
    return target[0] if isinstance(target, list) else target.split(".")[0]


def role_policy(template, function_id, which):
    """(the logical ID of the function's role, that of the role's one AWS::IAM::Policy or None if it has none)."""
    role = template["Resources"][function_id]["Properties"].get("Role")
    if not (isinstance(role, dict) and "Fn::GetAtt" in role):
        raise ValueError(f"the {which} template's {function_id} does not take the ARN of a role of its own")
    role = attribute_of(role)
    policies = [rid for rid, r in template["Resources"].items() if r.get("Type") == "AWS::IAM::Policy"
                and {"Ref": role} in r.get("Properties", {}).get("Roles", [])]
    if len(policies) > 1:
        raise ValueError(f"the {which} template has {len(policies)} policies on the role of {function_id}, not one")
    return role, (policies[0] if policies else None)


def references(value):
    """The logical IDs a template value refers to (Ref, Fn::GetAtt, Fn::Sub's ${...})."""
    out = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "Ref":
                out.add(str(inner))
            elif key == "Fn::GetAtt":
                out.add(attribute_of(value))
            elif key == "Fn::Sub":
                text, local = (inner[0], inner[1] if len(inner) > 1 else {}) if isinstance(inner, list) else (inner, {})
                out |= {part.split("}")[0].split(".")[0] for part in text.split("${")[1:] if not part.startswith("!")} - set(local)
                out |= references(list(local.values()))
            else:
                out |= references(inner)
    elif isinstance(value, list):
        for inner in value:
            out |= references(inner)
    return out


def maintenance_first(deployed, synthesized, name):
    """The deployed template with the maintenance function, its role's policy and the output naming its
    image as the synthesized template has them; nothing else changed."""
    out = copy.deepcopy(deployed)
    d_id, s_id = named_function(deployed, name, "deployed"), named_function(synthesized, name, "synthesized")
    (d_role, d_policy), (s_role, s_policy) = role_policy(deployed, d_id, "deployed"), role_policy(synthesized, s_id, "synthesized")
    if d_role != s_role:
        raise ValueError(f"{name}'s role is {s_role} in the synthesized template, {d_role} in the deployed one")
    if (d_policy is None) != (s_policy is None):
        raise ValueError(f"{name}'s role has a policy in only one of the templates")
    out["Resources"][d_id]["Properties"] = copy.deepcopy(synthesized["Resources"][s_id]["Properties"])
    changed = [out["Resources"][d_id]["Properties"]]
    if d_policy is not None:
        document = copy.deepcopy(synthesized["Resources"][s_policy]["Properties"]["PolicyDocument"])
        out["Resources"][d_policy]["Properties"]["PolicyDocument"] = document
        changed.append(document)
    image = synthesized.get("Outputs", {}).get("MaintenanceImage")
    if image is not None and "MaintenanceImage" in out.get("Outputs", {}):
        out["Outputs"]["MaintenanceImage"]["Value"] = copy.deepcopy(image["Value"])
        changed.append(image["Value"])
    known = set(out.get("Resources", {})) | set(out.get("Parameters", {})) | PSEUDO
    missing = sorted(references(changed) - known)
    if missing:
        raise ValueError(f"this version's {name} refers to {missing}, which the deployed stack does not have")
    return out


def main(args):
    if len(args) != 4 or args[0] != "maintenance-first":
        print(__doc__[__doc__.index("usage:"):], file=sys.stderr)
        return 2
    try:
        changed = maintenance_first(load(args[1]), load(args[2]), args[3])
    except (ValueError, TypeError, KeyError, IndexError) as e:
        print(f"serverless_template.py: {e}", file=sys.stderr)
        return 1
    # UTF-8, as CloudFormation measures a template; nothing escaped that the template does not escape.
    sys.stdout.buffer.write(json.dumps(changed, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
