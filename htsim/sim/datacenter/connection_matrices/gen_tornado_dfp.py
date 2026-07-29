#!/usr/bin/env python

# Generate a Dragonfly+ "group-tornado" traffic matrix: the Dragonfly+ analog
# of the classic fat-tree tornado pattern.
#
# In a fat-tree, tornado pairs node i with its twin i+N/2 in the other half
# of the tree, which is guaranteed to be maximally distant (forces every
# packet through the core) -- it's the standard worst case for load
# balancing, since it can't be routed without crossing the full network.
#
# Dragonfly+ has no "other half" in that sense: locality comes from leaf/
# group membership (group_id = host // (p*l)), not tree position, so a blind
# i -> i+N/2 host-index offset may or may not land in a different group.
# This generator instead pairs every host with a host in a group offset by
# half the group count (group g -> group (g + no_groups//2) % no_groups),
# which guarantees every flow crosses a global (inter-group) link -- the
# actual worst case for Dragonfly+ load balancing, and the adversarial
# pattern that motivates non-minimal/Valiant routing in the first place.
#
# python gen_tornado_dfp.py <filename> <nodes> <conns> <flowsize> <extrastarttime> <randseed> <p> <l>
# Parameters:
# <nodes>   number of nodes in the topology
# <conns>    number of active connections (<=nodes; a random subset of hosts sends)
# <flowsize>   size of the flows in bytes
# <extrastarttime>   How long in microseconds to space the start times over (start time will be random in between 0 and this time).  Can be a float.
# <randseed>   Seed for random number generator, or set to 0 for random seed
# <p>   hosts per leaf switch (Dragonfly+ topology parameter)
# <l>   leaf switches per group (Dragonfly+ topology parameter)
#
# p and l must match the Dragonfly+ topology this CM will be run against --
# either the -p/-l values you passed to the simulator, or (if you let the
# topology auto-size from -radix/-size) the p=l=k/2 values it printed at
# startup ("DragonFly+ constructor done, ... nodes created").

import os
import sys
from random import seed, shuffle

if len(sys.argv) != 9:
    print("Usage: python gen_tornado_dfp.py <filename> <nodes> <conns> <flowsize> "
          "<extrastarttime> <randseed> <p> <l>")
    sys.exit()
filename = sys.argv[1]
nodes = int(sys.argv[2])
conns = int(sys.argv[3])
flowsize = int(sys.argv[4])
extrastarttime = float(sys.argv[5])
randseed = int(sys.argv[6])
p = int(sys.argv[7])
l = int(sys.argv[8])

print("Nodes: ", nodes)
print("Connections: ", conns)
print("Flowsize: ", flowsize, "bytes")
print("ExtraStartTime: ", extrastarttime, "us")
print("Random Seed ", randseed)
print("Dragonfly+ p=", p, "l=", l)

group_size = p * l
if group_size <= 0:
    print("p and l must both be positive")
    sys.exit(1)

no_groups = (nodes + group_size - 1) // group_size  # ceil(nodes / group_size)
if no_groups < 2:
    print(f"Only {no_groups} group(s) worth of hosts (group_size=p*l={group_size}, "
          f"nodes={nodes}) -- tornado needs at least 2 groups to have an "
          f"'opposite' group. Reduce p/l or increase nodes.")
    sys.exit(1)

offset = no_groups // 2
print(f"group_size={group_size}  no_groups={no_groups}  opposite group offset={offset}")

if randseed != 0:
    seed(randseed)

# Bucket host addresses by the group they fall in.
groups = [[] for _ in range(no_groups)]
for host in range(nodes):
    groups[host // group_size].append(host)

# Every host's destination is drawn from its opposite group, via a random
# permutation of that group's hosts (so within a group-pair it's a clean
# bijection when the two groups are the same size; group_size divides nodes
# evenly except possibly the last, partially-filled group, which wraps).
dsts = [None] * nodes
for g in range(no_groups):
    g_opp = (g + offset) % no_groups
    dst_pool = groups[g_opp][:]
    shuffle(dst_pool)
    if not dst_pool:
        continue
    for i, src in enumerate(groups[g]):
        dsts[src] = dst_pool[i % len(dst_pool)]

# Select which `conns` of the `nodes` hosts actually send.
active_srcs = list(range(nodes))
shuffle(active_srcs)
active_srcs = active_srcs[:conns]

f = open(filename, "w")
print("Nodes", nodes, file=f)
print("Connections", conns, file=f)
for n, src in enumerate(active_srcs):
    out = (str(src) + "->" + str(dsts[src]) + " id " + str(n + 1) + " start "
           + str(int(extrastarttime * 1000000)) + " size " + str(flowsize))
    print(out, file=f)
f.close()
