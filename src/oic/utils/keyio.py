import json

__author__ = 'rohe0002'

import M2Crypto
import logging
import os
import urlparse
import sys
import traceback

from requests import request

from jwkest.jwk import x509_rsa_loads, base64_to_long
from jwkest.jwk import long_to_mpi
from jwkest.jwk import long_to_base64
from jwkest.jwk import mpi_to_long
from M2Crypto.util import no_passphrase_callback

KEYLOADERR = "Failed to load %s key from '%s' (%s)"
logger = logging.getLogger(__name__)

# ======================================================================
traceback.format_exception(*sys.exc_info())


def rsa_eq(key1, key2):
    # Check if two RSA keys are in fact the same
    if key1.n == key2.n and key1.e == key2.e:
        return True
    else:
        return False


def key_eq(key1, key2):
    if type(key1) == type(key2):
        if isinstance(key1, basestring):
            return key1 == key2
        elif isinstance(key1, M2Crypto.RSA.RSA):
            return rsa_eq(key1, key2)

    return False


def rsa_load(filename):
    """Read a PEM-encoded RSA key pair from a file."""
    return M2Crypto.RSA.load_key(filename, M2Crypto.util.no_passphrase_callback)


def rsa_loads(key):
    """Read a PEM-encoded RSA key pair from a string."""
    return M2Crypto.RSA.load_key_string(key,
                                        M2Crypto.util.no_passphrase_callback)


def ec_load(filename):
    return M2Crypto.EC.load_key(filename, M2Crypto.util.no_passphrase_callback)


def x509_rsa_load(txt):
    """ So I get the same output format as loads produces
    :param txt:
    :return:
    """
    return [("rsa", x509_rsa_loads(txt))]


class RedirectStdStreams(object):
    def __init__(self, stdout=None, stderr=None):
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr

    def __enter__(self):
        self.old_stdout, self.old_stderr = sys.stdout, sys.stderr
        self.old_stdout.flush()
        self.old_stderr.flush()
        sys.stdout, sys.stderr = self._stdout, self._stderr

    #noinspection PyUnusedLocal
    def __exit__(self, exc_type, exc_value, traceback):
        self._stdout.flush()
        self._stderr.flush()
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr


def uniq_ext(lst, keys):
    rkeys = {}
    for typ, key in lst:
        if typ == "rsa":
            rkeys[(key.n, key.e)] = key

    for typ, item in keys:
        if typ != "rsa":
            lst.append((typ, item))
        else:
            key = (item.n, item.e)
            if key not in rkeys:
                lst.append((typ, item))
                rkeys[key] = item

    return lst


class Key():
    members = ["kty", "alg", "use", "kid"]

    def __init__(self, kty="", alg="", use="", kid="", key=""):
        self.key = key
        self.kty = kty.lower()
        self.alg = alg
        self.use = use
        self.kid = kid

    def to_dict(self):
        res = {}
        for key in self.members:
            try:
                _val = getattr(self, key)
                if _val:
                    res[key] = _val
            except KeyError:
                pass
        return res

    def __str__(self):
        return str(self.to_dict())

    def comp(self):
        pass

    def decomp(self):
        pass

    def dc(self):
        pass


class RSA_key(Key):
    members = ["kty", "alg", "use", "kid", "n", "e"]

    def __init__(self, kty="rsa", alg="", use="", kid="", n="", e="", key=""):
        Key.__init__(self, kty, alg, use, kid, key)
        self.n = n
        self.e = e

    def comp(self):
        self.key = M2Crypto.RSA.new_pub_key(
            (long_to_mpi(base64_to_long(str(self.e))),
             long_to_mpi(base64_to_long(str(self.n)))))

    def decomp(self):
        self.n = long_to_base64(mpi_to_long(self.key.n))
        self.e = long_to_base64(mpi_to_long(self.key.e))

    def load(self, filename):
        self.key = rsa_load(filename)

    def dc(self):
        if self.key:
            self.decomp()
        elif self.n and self.e:
            self.comp()
        else:  # do nothing
            pass


class EC_key(Key):
    members = ["kty", "alg", "use", "kid", "crv", "x", "y"]

    def __init__(self, kty="rsa", alg="", use="", kid="", crv="", x="", y="",
                 key=""):
        Key.__init__(self, kty, alg, use, kid, key)
        self.crv = crv
        self.x = x
        self.y = y


class HMAC_key(Key):
    pass

K2C = {
    "rsa": RSA_key,
    "ec": EC_key,
    "hmac": HMAC_key
}


class KeyBundle(object):
    def __init__(self, keys=None, source="", cache_time=300, verify_ssl=True,
                 fileformat="jwk", keytype="rsa", keyusage=None):
        """

        :param keys: A list of dictionaries of the format
            with the keys ["kty", "key", "alg", "use", "kid"]
        :param source: Where the key set can be fetch from
        :param verify_ssl: Verify the SSL cert used by the server
        :param fileformat: For a local file either "jwk" or "der"
        :param keytype: Iff local file and 'der' format what kind of key is it.
        """

        self._keys = []
        self.remote = False
        self.verify_ssl = verify_ssl
        self.cache_time = cache_time
        self.time_out = 0
        self.etag = ""
        self.cache_control = None
        self.source = None
        self.fileformat = fileformat.lower()
        self.keytype = keytype.lower()
        self.keyusage = keyusage

        if keys:
            self.source = None
            if isinstance(keys, dict):
                self.do_keys([keys])
            else:
                self.do_keys(keys)
        else:
            if source.startswith("file://"):
                self.source = source[7:]
            elif source.startswith("http://") or source.startswith("https://"):
                self.source = source
                self.remote = True
            elif source == "":
                return
            else:
                raise Exception("Unsupported source type: %s" % source)

            if not self.remote:  # local file
                if self.fileformat == "jwk":
                    self.do_local_jwk(self.source)
                elif self.fileformat == "der":
                    self.do_local_der(self.source, self.keytype, self.keyusage)

    def do_keys(self, keys):
        """
        Go from JWK description to binary keys

        :param keys:
        :return:
        """
        for inst in keys:
            typ = inst["kty"].lower()
            _key = K2C[typ](**inst)
            _key.dc()
            self._keys.append(_key)

    def do_local_jwk(self, filename):
        self.do_keys(json.loads(open(filename).read())["keys"])

    def do_local_der(self, filename, keytype, keyusage):
        _bkey = None
        if keytype == "rsa":
            _bkey = rsa_load(filename)

        if not keyusage:
            keyusage = ["enc", "sig"]

        for use in keyusage:
            _key = K2C[keytype]()
            _key.key = _bkey
            _key.decomp()
            _key.use = use
            self._keys.append(_key)

    def do_remote(self):
        args = {"allow_redirects": True,
                "verify": self.verify_ssl,
                "timeout": 5.0}
        if self.etag:
            args["headers"] = {"If-None-Match": self.etag}

        r = request("GET", self.source, **args)

        if r.status_code == 304:  # file has not changed
            self.time_out = time.time() + self.cache_time
        elif r.status_code == 200:  # New content
            self.time_out = time.time() + self.cache_time

            self.do_keys(json.loads(r.text)["keys"])

            try:
                self.etag = r.headers["Etag"]
            except KeyError:
                pass
            try:
                self.cache_control = r.headers["Cache-Control"]
            except KeyError:
                pass

    def _uptodate(self):
        if self._keys is not []:
            if self.remote:  # verify that it's not to old
                if time.time() > self.time_out:
                    self.update()
        elif self.remote:
            self.update()

    def update(self):
        """
        Reload the key if necessary
        This is a forced update, will happen even if cache time has not elapsed
        """
        if self.source:
            # reread everything

            self._keys = []

            if self.remote is False:
                if self.fileformat == "jwk":
                    self.do_local_jwk(self.source)
                elif self.fileformat == "der":
                    self.do_local_der(self.source, self.keytype, self.keyusage)
            else:
                self.do_remote()

    def get(self, typ):
        """

        :param typ: Type of key (rsa, ec, hmac, ..)
        :return: If typ is undefined all the keys as a dictionary
            otherwise the appropriate keys in a list
        """
        self._uptodate()

        if typ:
            typ = typ.lower()
            return [k for k in self._keys if k.kty == typ]
        else:
            return self._keys

    def keys(self):
        self._uptodate()

        return self._keys

    def remove(self, typ, val=None):
        """

        :param typ: Type of key (rsa, ec, hmac, ..)
        :param val: The key itself
        """
        typ = typ.lower()

        if val:
            self._keys = [k for k in self._keys if
                          not (k.kty == typ and k.key == val)]
        else:
            self._keys = [k for k in self._keys if not k.kty == typ]

    def __str__(self):
        return str(self.jwks())

    def jwks(self):
        self._uptodate()
        return json.dumps({"keys": [k.to_dict() for k in self._keys]})

    def append(self, key):
        self._keys.append(key)


def keybundle_from_local_file(filename, typ, usage):
    if typ.lower() == "rsa":
        kb = KeyBundle()
        k = RSA_key()
        k.load(filename)
        k.use = usage[0]
        kb.append(k)
        for use in usage[1:]:
            _k = RSA_key()
            _k.key = k.key
            _k.use = use
            kb.append(_k)
    else:
        raise Exception("Unsupported key type")

    return kb


def dump_jwks(kbl, target):
    """
    Write a JWK to a file

    :param kbl: List of KeyBundles
    :param target: Name of the file to which everything should be written
    """
    res = {"keys": []}
    for kb in kbl:
        res["keys"].extend([k.to_dict() for k in kb.keys()])

    try:
        f = open(target, 'w')
    except IOError:
        (head, tail) = os.path.split(target)
        os.makedirs(head)
        f = open(target, 'w')

    _txt = json.dumps(res)
    f.write(_txt)
    f.close()


class KeyJar(object):
    """ A keyjar contains a number of KeyBundles """

    def __init__(self, ca_certs=None):
        self.spec2key = {}
        self.issuer_keys = {}
        self.ca_certs = ca_certs

    def add_if_unique(self, issuer, use, keys):
        if use in self.issuer_keys[issuer] and self.issuer_keys[issuer][use]:
            for typ, key in keys:
                flag = 1
                for _typ, _key in self.issuer_keys[issuer][use]:
                    if _typ == typ and key is _key:
                        flag = 0
                        break
                if flag:
                    self.issuer_keys[issuer][use].append((typ, key))
        else:
            self.issuer_keys[issuer][use] = keys

    def add(self, issuer, url):
        """

        :param issuer: Who issued the keys
        :param url: Where can the key/-s be found
        """

        if "/localhost:" in url or "/localhost/" in url:
            kc = KeyBundle(source=url, verify_ssl=False)
        else:
            kc = KeyBundle(source=url)

        try:
            self.issuer_keys[issuer].append(kc)
        except KeyError:
            self.issuer_keys[issuer] = [kc]

        return kc

    def add_hmac(self, issuer, key, usage):
        if not issuer in self.issuer_keys:
            self.issuer_keys[issuer] = []

        for use in usage:
            self.issuer_keys[""].append(KeyBundle([{"kty": "hmac",
                                                    "key": key,
                                                    "use": use}]))

    def add_kb(self, issuer, kb):
        try:
            self.issuer_keys[issuer].append(kb)
        except KeyError:
            self.issuer_keys[issuer] = [kb]

    def __setitem__(self, issuer, val):
        if isinstance(val, basestring):
            val = [val]
        elif not isinstance(val, list):
            val = [val]

        self.issuer_keys[issuer] = val

    def get(self, use, key_type="", issuer=""):
        """

        :param use: A key useful for this usage (enc, dec, sig, ver)
        :param key_type: Type of key (rsa, ec, hmac, ..)
        :param issuer: Who is responsible for the keys, "" == me
        :return: A possibly empty list of keys
        """

        if use == "dec":
            use = "enc"
        elif use == "ver":
            use = "sig"

        if issuer != "":
            try:
                _keys = self.issuer_keys[issuer]
            except KeyError:
                if issuer.endswith("/"):
                    try:
                        _keys = self.issuer_keys[issuer[:-1]]
                    except KeyError:
                        _keys = []
                else:
                    try:
                        _keys = self.issuer_keys[issuer + "/"]
                    except KeyError:
                        _keys = []
        else:
            _keys = self.issuer_keys[issuer]

        res = {}
        if _keys:
            if key_type:
                lst = []
                for bundles in _keys:
                    for key in bundles.get(key_type):
                        if use == key.use:
                            lst.append(key.key)
                res[key_type] = lst
            else:
                for bundles in _keys:
                    for key in bundles.keys():
                        if use == key.use:
                            try:
                                res[key.kty].append(key.key)
                            except KeyError:
                                res[key.kty] = [key.key]

        return res

    def get_signing_key(self, key_type="", owner=""):
        return self.get("sig", key_type, owner)

    def get_verify_key(self, key_type="", owner=""):
        return self.get("ver", key_type, owner)

    def get_encrypt_key(self, key_type="", owner=""):
        return self.get("enc", key_type, owner)

    def get_decrypt_key(self, key_type="", owner=""):
        return self.get("dec", key_type, owner)

    def __contains__(self, item):
        if item in self.issuer_keys:
            return True
        else:
            return False

    def x_keys(self, var, part):
        _func = getattr(self, "get_%s_key" % var)

        keys = _func(key_type="", owner=part)
        for kty, val in _func(key_type="", owner="").items():
            if not val:
                continue
            try:
                keys[kty].extend(val)
            except KeyError:
                keys[kty] = val
        return keys

    def verify_keys(self, part):
        """
        Keys for me and someone else.

        :param part: The other part
        :return: dictionary of keys
        """
        return self.x_keys("verify", part)

    def decrypt_keys(self, part):
        """
        Keys for me and someone else.

        :param part: The other part
        :return: dictionary of keys
        """

        return self.x_keys("decrypt", part)

    def __getitem__(self, issuer):
        return self.issuer_keys[issuer]

    def remove_key(self, issuer, key_type, key):
        try:
            kcs = self.issuer_keys[issuer]
        except KeyError:
            return

        for kc in kcs:
            kc.remove(key_type, key)
            if len(kc._keys) == 0:
                self.issuer_keys[issuer].remove(kc)

    def update(self, kj):
        for key, val in kj.issuer_keys.items():
            if isinstance(val, basestring):
                val = [val]
            elif not isinstance(val, list):
                val = [val]

            try:
                self.issuer_keys[key].extend(val)
            except KeyError:
                self.issuer_keys[key] = val

    def match_owner(self, url):
        for owner in self.issuer_keys.keys():
            if url.startswith(owner):
                return owner

        raise Exception("No keys for '%s'" % url)

    def __str__(self):
        _res = {}
        for k, vs in self.issuer_keys.items():
            _res[k] = [str(v) for v in vs]
        return "%s" % _res

    def keys(self):
        self.issuer_keys.keys()

    def load_keys(self, pcr, issuer, replace=False):
        """
        Fetch keys from another server

        :param pcr: The provider information
        :param issuer: The provider URL
        :param replace: If all previously gathered keys from this provider
            should be replace.
        :return: Dictionary with usage as key and keys as values
        """

        logger.debug("loading keys for issuer: %s" % issuer)
        logger.debug("pcr: %s" % pcr)
        if issuer not in self.issuer_keys:
            self.issuer_keys[issuer] = []
        elif replace:
            self.issuer_keys[issuer] = []

        try:
            self.add(issuer, pcr["jwks_uri"])
        except KeyError:
            pass


# =============================================================================


def key_setup(vault, **kwargs):
    """
    :param vault: Where the keys are kept
    :return: 2-tuple: result of urlsplit and a dictionary with
        parameter name as key and url and value
    """
    vault_path = proper_path(vault)

    if not os.path.exists(vault_path):
        os.makedirs(vault_path)

    kb = KeyBundle()
    kid = 1
    for usage in ["sig", "enc"]:
        if usage in kwargs:
            if kwargs[usage] is None:
                continue

            _args = kwargs[usage]
            if _args["alg"] == "rsa":
                try:
                    _key = rsa_load('%s%s' % (vault_path, "pyoidc"))
                except Exception:
                    devnull = open(os.devnull, 'w')
                    with RedirectStdStreams(stdout=devnull, stderr=devnull):
                        _key = create_and_store_rsa_key_pair(
                            path=vault_path)

                kb.append(RSA_key(key=_key, use=usage, kid=kid))
                kid += 1
                if usage == "sig" and "enc" not in kwargs:
                    kb.append(RSA_key(key=_key, use="enc", kid=kid))
                    kid += 1

    return kb


def key_export(baseurl, local_path, vault, keyjar, **kwargs):
    """
    :param baseurl: The base URL to which the key file names are added
    :param local_path: Where on the machine the export files are kept
    :param vault: Where the keys are kept
    :param keyjar: Where to store the exported keys
    :return: 2-tuple: result of urlsplit and a dictionary with
        parameter name as key and url and value
    """
    part = urlparse.urlsplit(baseurl)

    # deal with the export directory
    if part.path.endswith("/"):
        _path = part.path[:-1]
    else:
        _path = part.path[:]

    local_path = proper_path("%s/%s" % (_path, local_path))

    if not os.path.exists(local_path):
        os.makedirs(local_path)

    kb = key_setup(vault, **kwargs)

    try:
        keyjar[""].append(kb)
    except KeyError:
        keyjar[""] = kb

    # the local filename
    _export_filename = "%sjwks" % local_path

    f = open(_export_filename, "w")
    f.write("%s" % kb)
    f.close()

    _url = "%s://%s%s" % (part.scheme, part.netloc,
                          _export_filename[1:])

    return _url

# ================= create RSA key ======================


def create_and_store_rsa_key_pair(name="pyoidc", path=".", size=1024):
    #Seed the random number generator with 1024 random bytes (8192 bits)
    M2Crypto.Rand.rand_seed(os.urandom(size))

    key = M2Crypto.RSA.gen_key(size, 65537, lambda: None)

    if not path.endswith("/"):
        path += "/"

    key.save_key('%s%s' % (path, name), None, callback=no_passphrase_callback)
    key.save_pub_key('%s%s.pub' % (path, name))

    return key


def proper_path(path):
    """
    Clean up the path specification so it looks like something I could use.
    "./" <path> "/"
    """
    if path.startswith("./"):
        pass
    elif path.startswith("/"):
        path = ".%s" % path
    elif path.startswith("."):
        while path.startswith("."):
            path = path[1:]
        if path.startswith("/"):
            path = ".%s" % path
    else:
        path = "./%s" % path

    if not path.endswith("/"):
        path += "/"

    return path

# ================= create certificate ======================
# heavily influenced by
# http://svn.osafoundation.org/m2crypto/trunk/tests/test_x509.py

import time
from M2Crypto import EVP
from M2Crypto import X509
from M2Crypto import RSA
from M2Crypto import ASN1


def make_req(bits, fqdn="example.com", rsa=None):
    pk = EVP.PKey()
    x = X509.Request()
    if not rsa:
        rsa = RSA.gen_key(bits, 65537, lambda: None)
    pk.assign_rsa(rsa)
    # Because rsa is messed with
    rsa = pk.get_rsa()
    x.set_pubkey(pk)
    name = x.get_subject()
    name.C = "SE"
    name.CN = "OpenID Connect Test Server"
    if fqdn:
        ext1 = X509.new_extension('subjectAltName', fqdn)
        extstack = X509.X509_Extension_Stack()
        extstack.push(ext1)
        x.add_extensions(extstack)
    x.sign(pk, 'sha1')
    return x, pk, rsa


def make_cert(bits, fqdn="example.com", rsa=None):
    req, pk, rsa = make_req(bits, fqdn=fqdn, rsa=rsa)
    pkey = req.get_pubkey()
    sub = req.get_subject()
    cert = X509.X509()
    cert.set_serial_number(1)
    cert.set_version(2)
    cert.set_subject(sub)
    t = long(time.time()) + time.timezone
    now = ASN1.ASN1_UTCTIME()
    now.set_time(t)
    nowPlusYear = ASN1.ASN1_UTCTIME()
    nowPlusYear.set_time(t + 60 * 60 * 24 * 365)
    cert.set_not_before(now)
    cert.set_not_after(nowPlusYear)
    issuer = X509.X509_Name()
    issuer.CN = 'The code tester'
    issuer.O = 'Umea University'
    cert.set_issuer(issuer)
    cert.set_pubkey(pkey)
    cert.sign(pk, 'sha1')
    return cert, rsa
