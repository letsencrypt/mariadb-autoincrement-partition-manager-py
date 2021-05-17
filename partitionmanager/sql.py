"""
Interact with SQL databases.
"""

from collections import defaultdict
import logging
import subprocess
import xml.parsers.expat

import pymysql
import pymysql.cursors

import partitionmanager.types


def _destring(text):
    """Try and get a python type from a string. Used for SQL results."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class XmlResult:
    """Parses XML results from the mariadb CLI client.

    The general schema is:
    <resultset statement="sql query">
        <row>
            <field name="name" xsi:nil="true/false">data if any</field>
        </row>
    </resultset>

    The major hangups are that field can be nil, and field can also be
    of arbitrary size.
    """

    def __init__(self):
        self.logger = logging.getLogger("xml")

        # The XML debugging is a little much, normally. If we're debugging
        # the parser, comment this out or set it to DEBUG.
        self.logger.setLevel("INFO")

        self.xmlparser = xml.parsers.expat.ParserCreate()

        self.xmlparser.StartElementHandler = self._start_element
        self.xmlparser.EndElementHandler = self._end_element
        self.xmlparser.CharacterDataHandler = self._char_data

        self.rows = None
        self.current_row = None
        self.current_field = None
        self.current_elements = list()
        self.statement = None

    def parse(self, data):
        """Return rows from an XML Result object."""
        if self.rows is not None:
            raise ValueError("XmlResult objects can only be used once")

        self.rows = list()
        self.xmlparser.Parse(data)

        if self.current_elements:
            raise partitionmanager.types.TruncatedDatabaseResultException(
                f"These XML tags are unclosed: {self.current_elements}"
            )
        return self.rows

    def _start_element(self, name, attrs):
        self.logger.debug(
            f"Element start: {name} {attrs} (Current elements: {self.current_elements}"
        )
        self.current_elements.append(name)

        if name == "resultset":
            self.statement = attrs["statement"]
        elif name == "row":
            assert self.current_row is None
            self.current_row = defaultdict(str)
        elif name == "field":
            assert self.current_field is None
            self.current_field = attrs["name"]
            if "xsi:nil" in attrs and attrs["xsi:nil"] == "true":
                self.current_row[attrs["name"]] = None

    def _end_element(self, name):
        self.logger.debug(
            f"Element end: {name} (Current elements: {self.current_elements}"
        )
        assert name == self.current_elements.pop()

        if name == "row":
            self.rows.append(self.current_row)
            self.current_row = None
        elif name == "field":
            assert self.current_field is not None
            value = self.current_row[self.current_field]
            if value:
                self.current_row[self.current_field] = _destring(value)
            self.current_field = None

    def _char_data(self, data):
        if self.current_elements[-1] == "field":
            assert self.current_field is not None
            assert self.current_row is not None

            self.current_row[self.current_field] += data


class SubprocessDatabaseCommand(partitionmanager.types.DatabaseCommand):
    """Run a database command via the CLI tool, getting the results in XML form.

    This can be very convenient without explicit port-forwarding, but is a
    little slow.
    """

    def __init__(self, exe):
        self.exe = exe

    def run(self, sql_cmd):
        result = subprocess.run(
            [self.exe, "-X"],
            input=sql_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="UTF-8",
            check=True,
        )
        return XmlResult().parse(result.stdout)

    def db_name(self):
        rows = self.run("SELECT DATABASE();")
        if len(rows) != 1:
            raise partitionmanager.types.TableInformationException(
                "Expected one result"
            )

        return partitionmanager.types.SqlInput(rows[0]["DATABASE()"])


class IntegratedDatabaseCommand(partitionmanager.types.DatabaseCommand):
    """Run a database command via a direct socket connection and pymysql.

    Pymysql is a pure Python PEP 249-compliant database connector.
    """

    def __init__(self, url):
        self.db = None
        if url.path and url.path != "/":
            self.db = url.path.lstrip("/")
        if not self.db:
            raise Exception("You must supply a database name")

        self.connection = pymysql.connect(
            host=url.hostname,
            port=url.port,
            user=url.username,
            password=url.password,
            database=self.db,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def db_name(self):
        return partitionmanager.types.SqlInput(self.db)

    def run(self, sql_cmd):
        with self.connection.cursor() as cursor:
            cursor.execute(sql_cmd)
            return [row for row in cursor]
