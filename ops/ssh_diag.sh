#!/bin/bash
echo "=== 1. sshd status ==="
systemctl status sshd --no-pager | head -15

echo ""
echo "=== 2. sshd listening ==="
ss -tlnp 2>/dev/null | grep :22

echo ""
echo "=== 3. sshd_config limits ==="
grep -E "^(MaxStartups|MaxSessions|ListenAddress|AddressFamily|Port)" /etc/ssh/sshd_config 2>/dev/null

echo ""
echo "=== 4. TCP wrappers ==="
echo "--- hosts.allow ---"; cat /etc/hosts.allow 2>/dev/null
echo "--- hosts.deny ---"; cat /etc/hosts.deny 2>/dev/null

echo ""
echo "=== 5. fail2ban / sshguard ==="
systemctl status fail2ban sshguard 2>&1 | grep -E "Active|not-found|could not be found"

echo ""
echo "=== 6. iptables/nftables DROP or REJECT rules ==="
sudo iptables -L -n -v 2>/dev/null | grep -E "DROP|REJECT"
sudo nft list ruleset 2>/dev/null | grep -E "drop|reject"

echo ""
echo "=== 7. Wi-Fi link quality + recent disconnect events (last 2h) ==="
nmcli device show wlp1s0 2>/dev/null | grep -iE "signal|state"
journalctl -k --since "-2 hours" 2>/dev/null | grep -iE "wlp1s0|disassoc|deauth|link is not ready" | tail -20

echo ""
echo "=== 8. sshd restarts/crashes recently ==="
journalctl -u sshd --since "-2 hours" 2>/dev/null | grep -iE "start|stop|fail|restart|error"

echo ""
echo "=== 9. System time ==="
date
timedatectl 2>/dev/null | grep -iE "local time|ntp|sync"

echo ""
echo "=== 10. Recent auth log for failed/dropped connections ==="
journalctl -u sshd --since "-2 hours" --no-pager 2>/dev/null | tail -40

echo ""
echo "=== DONE — скопируй весь вывод выше и пришли ==="
