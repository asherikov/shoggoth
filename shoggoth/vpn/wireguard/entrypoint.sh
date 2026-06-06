#!/usr/bin/env bash
set -euo pipefail

# wg-easy Dockerfile sets iptables-legacy as default via update-alternatives.
# Legacy backend fails on modern nftables kernels (no nat table).
# Repoint the alternatives symlinks to the nft backend.
ln -sf /usr/sbin/iptables-nft /etc/alternatives/iptables
ln -sf /usr/sbin/iptables-nft-restore /etc/alternatives/iptables-restore
ln -sf /usr/sbin/iptables-nft-save /etc/alternatives/iptables-save
ln -sf /usr/sbin/ip6tables-nft /etc/alternatives/ip6tables
ln -sf /usr/sbin/ip6tables-nft-restore /etc/alternatives/ip6tables-restore
ln -sf /usr/sbin/ip6tables-nft-save /etc/alternatives/ip6tables-save

exec "$@"