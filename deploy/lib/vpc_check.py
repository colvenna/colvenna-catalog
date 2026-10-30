#!/usr/bin/env python3
"""What the serverless form's preflight (deploy/lib/serverless.sh) decides about the user's VPC and
database, from the AWS CLI's JSON, which it reads on stdin. Read-only: it only judges.

usage:
  vpc_check.py network <region> <vpc-id> <subnet-id>,<subnet-id>...
      stdin: {"vpc": describe-vpcs, "subnets": describe-subnets, "tables": describe-route-tables,
              "endpoints": describe-vpc-endpoints of the VPC, "api": describe-vpc-endpoints of the
              execute-api endpoints given}
      prints {"problems": [...], "cidr": ..., "zones": "<zone>,...", "endpoints": "<vpce>,..."}:
      the zones in the subnets' order, the execute-api endpoints with one in the VPC first
  vpc_check.py database <port> <vpc-id> <catalog security group, or "">
      stdin: {"groups": describe-security-groups of the database, "subnets": describe-subnets}
      prints open, closed, or "vpc <ids>" when the database's groups are of another VPC
"""
import ipaddress
import json
import sys


def network(region, vpc_id, order, d):
    """The VPC's fitness for the serverless form: private subnets in two zones or more; STS, Cognito
    identity (interface endpoints with private DNS) and S3 (a gateway endpoint on every subnet's route
    table) reachable from them, or a NAT gateway as every subnet's default route; and the execute-api
    endpoints, one of them in the VPC."""
    problems = []
    vpc = d["vpc"]["Vpcs"][0]
    subnets = {s["SubnetId"]: s for s in d["subnets"]["Subnets"]}
    for sid in order:
        subnet = subnets[sid]
        if subnet["VpcId"] != vpc_id:
            problems.append(f"{sid} is in {subnet['VpcId']}, not {vpc_id}")
        if subnet.get("MapPublicIpOnLaunch"):
            problems.append(f"{sid} gives public addresses: the catalog runs in private subnets")
    if len({subnets[s]["AvailabilityZone"] for s in order}) < 2:
        problems.append("COLVENNA_SUBNET_IDS: the subnets must span two zones or more")

    route_tables = d["tables"]["RouteTables"]
    main = next((t for t in route_tables if any(a.get("Main") for a in t.get("Associations", []))), None)

    def table(sid):
        for t in route_tables:
            if any(a.get("SubnetId") == sid for a in t.get("Associations", [])):
                return t
        return main

    tables = {sid: table(sid) for sid in order}
    nat = all(t is not None and any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("NatGatewayId")
                                    and r.get("State") == "active" for r in t.get("Routes", []))
              for t in tables.values())
    live = [e for e in d["endpoints"]["VpcEndpoints"] if e.get("State", "").lower() == "available"]
    for service in ("sts", "cognito-identity"):
        name = f"com.amazonaws.{region}.{service}"
        if not nat and not any(e["ServiceName"] == name and e["VpcEndpointType"] == "Interface"
                               and e.get("PrivateDnsEnabled") for e in live):
            problems.append(f"{service}: no interface endpoint with private DNS in {vpc_id}, and no NAT gateway route")
    s3 = [e for e in live if e["ServiceName"] == f"com.amazonaws.{region}.s3" and e["VpcEndpointType"] == "Gateway"]
    missing = [sid for sid, t in tables.items()
               if t is None or not any(t["RouteTableId"] in e.get("RouteTableIds", []) for e in s3)]
    if missing and not nat:
        problems.append("s3: no gateway endpoint on the route tables of " + ",".join(missing) + ", and no NAT gateway route")

    mine = []
    for e in d["api"]["VpcEndpoints"]:
        eid, state = e["VpcEndpointId"], e.get("State", "")
        if e["ServiceName"] != f"com.amazonaws.{region}.execute-api" or e["VpcEndpointType"] != "Interface":
            problems.append(f"{eid} is not an execute-api interface endpoint")
        elif state.lower() != "available":
            problems.append(f"{eid} is {state}, not available")
        elif e["VpcId"] == vpc_id:
            mine.append(eid)
    if not mine:
        problems.append(f"COLVENNA_API_ENDPOINT_IDS: none is in {vpc_id}, where install.sh checks the API from")
    given = [e["VpcEndpointId"] for e in d["api"]["VpcEndpoints"]]
    ordered = mine[:1] + [i for i in given if i not in mine[:1]]
    return {"problems": problems, "cidr": vpc["CidrBlock"],
            "zones": ",".join(subnets[s]["AvailabilityZone"] for s in order), "endpoints": ",".join(ordered)}


def database(port, vpc_id, catalog_group, d):
    """Whether the database's security groups admit the port from the catalog's functions: from CIDRs
    holding every one of the catalog's subnets, or from the functions' security group."""
    groups = d["groups"]["SecurityGroups"]
    others = sorted({g["VpcId"] for g in groups if g["VpcId"] != vpc_id})
    if others:
        return "vpc " + ",".join(others)
    needed = [ipaddress.ip_network(s["CidrBlock"]) for s in d["subnets"]["Subnets"]]
    cidrs, admitted = [], False
    for group in groups:
        for permission in group.get("IpPermissions", []):
            protocol = permission["IpProtocol"]
            if protocol != "-1" and not (protocol == "tcp" and permission["FromPort"] <= port <= permission["ToPort"]):
                continue
            admitted |= bool(catalog_group) and any(pair.get("GroupId") == catalog_group
                                                    for pair in permission.get("UserIdGroupPairs", []))
            cidrs += [ipaddress.ip_network(r["CidrIp"]) for r in permission.get("IpRanges", [])]
    admitted |= bool(needed) and all(any(s.subnet_of(c) for c in cidrs) for s in needed)
    return "open" if admitted else "closed"


def main(args):
    data = json.load(sys.stdin)
    if len(args) == 4 and args[0] == "network":
        print(json.dumps(network(args[1], args[2], args[3].split(","), data)))
    elif len(args) == 4 and args[0] == "database":
        print(database(int(args[1]), args[2], args[3], data))
    else:
        print(__doc__.split("usage:\n", 1)[1], file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
