#!/usr/bin/env python3
# coding: utf8
#
# Copyright (c) 2019 Andy Durant <andy@ajdurant.co.uk>
# Copyright (c) 2016 Edwin Fine <emofine@usa.net>
# Copyright (c) 2016 Travis Cross <tc@traviscross.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
Usage:
  gen-orgchart [options] <ldapuri> <basedn>
  gen-orgchart -h | --help | -V | --version

Generate an org chart in Graphviz DOT output format.

The org chart hierarchy is based on the 'manager' LDAP attribute.
Other required attributes are, by schema:

inetOrgPerson       Active Directory
-------------       ----------------
displayName         displayName
title               title
o                   company
departmentNumber    department

Arguments:
  <ldapuri>  LDAP URI (e.g. ldaps://ad.example.com)
  <basedn>   LDAP Base DN (e.g. dc=example,dc=com)

Options:
  -D, --binddn=BINDDN            LDAP Bind DN (e.g. alice@example.com)
  -d, --debug                    Output debug information to stderr.
  -h, --help                     Show this help.
  -o OUTFILE, --output=OUTFILE   Output file (stdout if omitted).
  --schema=SCHEMA                Define the schema to use (case-
                                 insensitive). Choices are:
                                 inetOrgPerson, ActiveDirectory
                                 [default: ActiveDirectory].
  -t TIMEOUT, --timeout=TIMEOUT  Timeout in seconds (-1: forever)
                                 [default: -1]
  --trace-level=TRACELEVEL       Trace method calls with arguments and
                                 results; range is 0 to 9 [default: 0].
  -V, --version                  Show version.
  -v, --verbose                  Print extra information [default: False].
  -S, --starttls                 Use STARTTLS.
  -C CAFILE, --cafile=CAFILE     File location of CA certificate to use
                                 for verification.
  -W, --askpass                  Prompt for simple authentication.
                                 This is used instead of specifying the
                                 password on the command line.
  -w, --password=BINDPASSWORD    LDAP simple bind password
  -p PFILE, --passfile=PFILE     File location of password to use
                                 for bind.
"""

import contextlib
import ldif3
import re
import sys

from getpass import getpass

try:
    from docopt import docopt
except ImportError:
    exit('This program requires that the `docopt` library'
         ' is installed: \n    pip install docopt\n'
         'https://pypi.python.org/pypi/docopt/')

try:
    from jinja2 import Environment
except ImportError:
    exit('This program requires that the `jinja2` library'
         ' is installed: \n    pip install jinja2\n'
         'http://pypi.python.org/pypi/jinja2/')

try:
    import ldap
    from ldap.controls.libldap import SimplePagedResultsControl
except ImportError:
    exit('This program requires that the `python-ldap` library'
         ' is installed: \n    pip install python-ldap\n'
         'http://pypi.python.org/pypi/python-ldap/')

# To canonicalize LDAP attributes.
MAP_ATTRS = {
    "department": "departmentNumber",
    "comment": "description",
    "company": "o",
    "employeeID": "employeeNumber",
    "thumbnailPhoto": "jpegPhoto",
    "streetaddress": "street",
}

# Jinja2 orgchart template string
ORGCHART_TEMPLATE_STR = """
strict digraph orgchart {
  fontsize = 10;
  fontname = "Helvetica";
  node [
    shape = box,
    fontname = "Helvetica",
    fontsize = 10
  ];
  {% for dept in depts.keys() %}
  subgraph cluster_{{ dept | snake_case }} {
    label = "{{ dept }}";
    {%- for dn in depts[dept] %}
    "{{ dn | name_and_title(display_name, title_of) }}";
    {%- endfor %}
  }
  {% endfor %}
  {%- for mgr_dn in reports_to.keys() %}
  "{{ mgr_dn | name_and_title(display_name, title_of) }}" -> {
    {%- for dn in reports_to[mgr_dn] %}
    "{{ dn | name_and_title(display_name, title_of) }}"
    {%- endfor %}
  }
  {% endfor %}
}

"""


def canonicalize_attrs(entry, attr_map):
    """
    Canonicalize AD attr names to inetOrgPerson schema.

    :param entry
    """
    for attr in entry:
        if attr in attr_map:
            entry[attr_map[attr]] = entry.pop(attr)
    return entry


def stderr(*args):
    """Log to stderr"""
    print(args, file=sys.stderr)


def filters_to_ldapfilter(filters):
    """
    Convert dict to a simple LDAP '&' filter string.

    :param dict filters: The dictionary of filter expressions to be
        ANDed together.

        This should be in the form {b"attr_name": b"filter_expr"},
        where `attr_name` is the LDAP attribute name to be used in
        the filter expression, and `filter_expr` is a 3-tuple:

        (b"prefix_op", b"compare_op", b"val")

        This tuple represents a filter like `(sn="Smith")` or
        `(!(manager=""))`. It's not very sophisticated but it gets
        the job done.

        `prefix_op` is either `None` or a prefix operator, currently
        only expected to be '!'.
        `compare_op` is a comparison operator such as '=' or '>'.
        `val` is the value to be used in the comparison.

    :returns: LDAP filter
    :rtype: str
    """

    filt_strs = []
    ldap_filter = ''

    for attr, (prefix_op, compare_op, val) in filters.items():
        if prefix_op:
            filt_str = f"({prefix_op}({attr}{compare_op}{val}))"
        else:
            filt_str = f"({attr}{compare_op}{val})"
        filt_strs.append(filt_str)

    if len(filt_strs) > 1:
        ldap_filter = f"(&{''.join(filt_strs)})"
    else:
        if len(filt_strs) == 1:
            ldap_filter = f"({filt_strs[0]})"

    return ldap_filter


def save_result(output, result):
    ldif_writer = ldif3.LDIFWriter(output)
    for dn, entry in result:
        if dn is not None:
            ldif_writer.unparse(dn, entry)


def result_to_org(result0, preprocess=None):
    reports_to = {}
    depts = {}
    dept_of = {}
    title_of = {}
    mgr_of = {}
    display_name = {}

    if preprocess:
        result = preprocess(result0)
    else:
        result = result0

    for dn, entry in result:
        if dn is not None:
            canonicalize_attrs(entry, MAP_ATTRS)
            display_name[dn] = attr_s(entry, "displayName")
            title = attr_s(entry, "title")

            mgr = attr_s(entry, "manager")
            if mgr:
                mgr_of[dn] = mgr
                reports_to.setdefault(mgr, []).append(dn)

            dept = attr_s(entry, "departmentNumber")

            if dept:
                depts.setdefault(dept, []).append(dn)
                dept_of[dn] = dept

            title_of[dn] = title

    return {
            "reports_to": reports_to,
            "depts": depts,
            "dept_of": dept_of,
            "title_of": title_of,
            "mgr_of": mgr_of,
            "display_name": display_name,
           }


def dn_to_ident(dn_str):
    """Return the first component of a DN"""
    return ldap.dn.explode_dn(dn_str, 1)[0]


def attr_s(entry, attr_name):
    """Get the first attribute named by `attr_name` out of `entry`.
    Return the attribute, or an empty string if not found."""
    if attr_name in entry and len(entry[attr_name]) != 0:
        return entry[attr_name][0].decode("utf-8")
    return ""


def title_s(dn, title_of):
    return title_of.get(dn, '(missing from database)')


def name_s(dn, display_name):
    return display_name.get(dn, dn_to_ident(dn))


def gen_orgchart(fh, org, jinja2_template_str):
    """Generate and output an orgchart using Jinja2 templating.

    :param file fh: Open output file handle
    :param dict org: The dict containing the org, looking like this:

    {
       "reports_to": reports_to,
       "depts" : depts,
       "dept_of": dept_of,
       "title_of": title_of,
       "mgr_of": mgr_of,
       "display_name": display_name,
    }

    Each of these is a dict of {b"dn": b"value"}.
    :param str jinja2_template_str: A Jinja2 template string.
    """

    def snake_case(s):
        return re.sub(r"\W+", "_", s).strip("_").lower()

    def name_and_title(dn, display_name, title_of):
        return f"{name_s(dn, display_name)}\n{title_s(dn, title_of)}"

    env = Environment()
    env.filters['snake_case'] = snake_case
    env.filters['name_and_title'] = name_and_title

    env.from_string(jinja2_template_str).stream(org).dump(fh)


def set_options(ldap_conn, ldap_opts):
    for opt, val in ldap_opts.items():
        ldap_conn.set_option(opt, val)


@contextlib.contextmanager
def smart_open(filename=None):
    """Open file for output, usable in `with`.

    Close the file on exit from the scope.
    """
    if filename and filename != '-':
        fh = open(filename, 'w', encoding='utf-8')
    else:
        fh = sys.stdout

    try:
        yield fh
    finally:
        if fh is not sys.stdout:
            fh.close()


def get_attrlist_filterstr(schema_name, filters):
    """Return a suitable attribute list and filter string for
    given schema.

    :param str schema_name: This is either b"inetOrgPerson" or
        b"ActiveDirectory" (case insensitive).
    :param dict filters: A dict of filter tuples.
        See :func:filters_to_ldapfilter for a description.
    :returns: (attrlist, filterstr)
    """

    s = schema_name.lower()

    if s == 'inetorgperson':
        attrstr = 'dn displayname title manager o departmentNumber'
    elif s == 'activedirectory':
        filters["sAMAccountType"] = (None, "=", "805306368")
        filters["userAccountControl:1.2.840.113556.1.4.803:"] = ("!", "=", "2")
        attrstr = 'dn displayname title manager company department'
    else:
        raise ValueError('Unsupported schema "{}"'.format(schema_name))

    return attrstr.split(), filters_to_ldapfilter(filters)


def ldap_errmsg(ldap_error):
    """Return a formatted string representation of an LDAPError.
    If ldap_error is not an instance of LDAPError, return its
    default string representation.
    """

    if not isinstance(ldap_error, ldap.LDAPError):
        return str(ldap_error)

    d = ldap_error.args[0]
    try:
        errmsg = d['desc']
    except:
        return str(ldap_error)
    if 'info' in d:
        errmsg = errmsg + ': ' + d['info']
    return errmsg


def main(**kwargs):
    ldapuri = kwargs["<ldapuri>"]
    basedn = kwargs["<basedn>"]

    debug = kwargs["--debug"]
    verbose = kwargs["--verbose"]
    output_file = kwargs["--output"]
    schema = kwargs["--schema"]
    binddn = kwargs["--binddn"]
    password = kwargs["--password"]
    askpass = kwargs["--askpass"]
    starttls = kwargs["--starttls"]
    cafile = kwargs["--cafile"]
    passfile = kwargs["--passfile"]
    trace_level = int(kwargs["--trace-level"])
    timeout = int(kwargs["--timeout"])

    if debug:
        print(f"askpass: {askpass}")

    if askpass:
        password = getpass("Bind password: ")

    if passfile:
        with open(passfile) as f:
            password = f.readline().strip()

    ldap_opts = {
        ldap.OPT_PROTOCOL_VERSION: 3,
        ldap.OPT_REFERRALS: 0,  # No, don't chase referrals
    }

    if cafile:
        # CA File requires setting globally
        ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, cafile)

    if timeout >= 0:
        ldap_opts[ldap.OPT_NETWORK_TIMEOUT] = timeout

    filters = {
        "displayName": (None, "=", "*"),
        "title": (None, "=", "*"),
    }

    attrlist, filterstr = get_attrlist_filterstr(schema, filters)

    ldap_conn = ldap.initialize(ldapuri, trace_level=trace_level)
    set_options(ldap_conn, ldap_opts)

    if starttls:
        ldap_conn.start_tls_s()

    if binddn is not None and password is not None:
        try:
            ldap_conn.simple_bind_s(binddn, password)
        except ldap.LDAPError as e:
            sys.exit(ldap_errmsg(e))

    serverctrls = [SimplePagedResultsControl(size=2147483647, cookie='')]

    try:
        result = ldap_conn.search_ext_s(basedn,
                                        ldap.SCOPE_SUBTREE,
                                        filterstr=filterstr,
                                        serverctrls=serverctrls,
                                        attrlist=attrlist,
                                        )
    except ldap.LDAPError as e:
        sys.exit(ldap_errmsg(e))

    if result is not None:
        with smart_open(output_file) as f:
            gen_orgchart(f, result_to_org(result), ORGCHART_TEMPLATE_STR)


if __name__ == "__main__":
    VERSION = '1.0'
    arguments = docopt(__doc__,
                       version='gen-orgchart {}'.format(VERSION),
                       options_first=True)
    if arguments["--verbose"]:
        stderr(arguments, "\n")

    main(**arguments)
