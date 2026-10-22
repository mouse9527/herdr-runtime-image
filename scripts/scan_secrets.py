"""Fail if anything under the given roots looks like a real credential.

  python3 scan_secrets.py <root> [<root>...]

Patterns need the full shape of a live credential (prefix + body length/charset), so library code that only
mentions a prefix (e.g. a CLI validating "sk-ant-") does not match. Findings print the path, pattern name and byte
offset, never the matched text. Also fails on SSH host keys baked into /etc/ssh.
"""
import os, re, stat, sys

PATTERNS = {
    'github-token': rb'(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9_])',
    'github-pat': rb'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}',
    'anthropic-key': rb'sk-ant-(?:api|admin|oat)[0-9]{2}-[A-Za-z0-9_-]{80,}',
    'openai-key': rb'sk-[A-Za-z0-9_-]{16,}T3BlbkFJ[A-Za-z0-9_-]{16,}',
    'aws-access-key': rb'(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])',
    'google-api-key': rb'(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])',
    'slack-token': rb'xox[abprs]-[0-9]{8,}-[0-9A-Za-z-]{20,}',
    'private-key': rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{200,}',
}
# Documented placeholder values that match the shapes above.
ALLOWED = {b'AKIAIOSFODNN7EXAMPLE', b'AKIAI44QH8DHBEXAMPLE'}
# (pattern, path regex): Debian's GnuTLS embeds fixed public test keys for its crypto self-tests (gnutls_pk_self_test,
# next to the 'test_sig' string). They ship in every Debian system and are not credentials.
ALLOWED_PATHS = [('private-key', re.compile(r'^/usr/lib/[^/]+-linux-gnu/libgnutls\.so\.30(\.[0-9]+)*$'))]
SKIP_DIRS = {'/proc', '/sys', '/dev', '/run'}
SKIP_NAMES = {'.git', '__pycache__', '.pytest_cache', 'node_modules'} if os.environ.get('SCAN_REPO') else {'.git'}
MAX_BYTES = 512 * 1024 * 1024
COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}


def scan_file(path):
    found = []
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0 or st.st_size > MAX_BYTES:
            return found
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return found
    for name, rx in COMPILED.items():
        for m in rx.finditer(data):
            if m.group(0) in ALLOWED or any(n == name and rx_path.match(path) for n, rx_path in ALLOWED_PATHS):
                continue
            found.append((path, name, m.start()))
    return found


def main(roots):
    findings, files = [], 0
    for root in roots:
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if os.path.join(d, x) not in SKIP_DIRS and x not in SKIP_NAMES]
            for n in names:
                files += 1
                findings += scan_file(os.path.join(d, n))
    if os.path.isdir('/etc/ssh') and '/' in roots:
        findings += [(os.path.join('/etc/ssh', n), 'ssh-host-key', 0)
                     for n in os.listdir('/etc/ssh') if n.startswith('ssh_host_')]
    for path, name, offset in findings:
        print('FOUND %s in %s at byte %d' % (name, path, offset))
    print('scanned %d files under %s: %d finding(s)' % (files, ' '.join(roots), len(findings)))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:] or ['.']))
