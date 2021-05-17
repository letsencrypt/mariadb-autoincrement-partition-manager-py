import unittest
from .sql import _destring, XmlResult
from .types import TruncatedDatabaseResultException


class TestSubprocessParsing(unittest.TestCase):
    def test_destring(self):
        self.assertEqual(_destring("not a number"), "not a number")
        self.assertEqual(_destring("99999"), 99999)
        self.assertEqual(_destring("999.99"), 999.99)
        self.assertEqual(_destring("9.9999"), 9.9999)
        self.assertEqual(_destring("1/2"), "1/2")
        self.assertEqual(_destring("NULL"), "NULL")

    def test_single_row(self):
        o = XmlResult().parse(
            """<?xml version="1.0"?>

<resultset statement="select * from authz2 limit 1" xmlns:xsi="">
  <row>
    <field name="id">1</field>
    <field name="identifierType">1</field>
    <field name="identifierValue">2</field>
    <field name="registrationID">3</field>
    <field name="status">4</field>
    <field name="expires">2021-02-03 17:48:59</field>
    <field name="challenges">0</field>
    <field name="attempted" xsi:nil="true" />
    <field name="attemptedAt" xsi:nil="true" />
    <field name="token">bogus                           </field>
    <field name="validationError" xsi:nil="true" />
    <field name="validationRecord" xsi:nil="true" />
  </row>
</resultset>"""
        )
        self.assertEqual(len(o), 1)
        d = o[0]
        self.assertEqual(d["id"], 1)
        self.assertEqual(d["identifierType"], 1)
        self.assertEqual(d["identifierValue"], 2)
        self.assertEqual(d["registrationID"], 3)
        self.assertEqual(d["status"], 4)
        self.assertEqual(d["expires"], "2021-02-03 17:48:59")
        self.assertEqual(d["challenges"], 0)
        self.assertEqual(d["attempted"], None)
        self.assertEqual(d["attemptedAt"], None)
        self.assertEqual(d["token"], "bogus                           ")
        self.assertEqual(d["validationError"], None)
        self.assertEqual(d["validationRecord"], None)

    def test_four_rows(self):
        o = XmlResult().parse(
            """<?xml version="1.0"?>

<resultset statement="select * from requestedNames limit 4" xmlns:xsi="">
  <row>
    <field name="id">1</field>
    <field name="orderID">1</field>
    <field name="reversedName">wtf.bogus.3c18ed9212e0</field>
  </row>

  <row>
    <field name="id">2</field>
    <field name="orderID">1</field>
    <field name="reversedName">wtf.bogus.8915c54c38d8</field>
  </row>

  <row>
    <field name="id">3</field>
    <field name="orderID">1</field>
    <field name="reversedName">wtf.bogus.86c81cfd8489</field>
  </row>

  <row>
    <field name="id">4</field>
    <field name="orderID">1</field>
    <field name="reversedName">wtf.bogus.74ce949b17da</field>
  </row>
</resultset>
"""
        )
        self.assertEqual(len(o), 4)
        for n, x in enumerate(o, start=1):
            self.assertEqual(x["id"], n)
            self.assertEqual(x["orderID"], 1)
            self.assertTrue("wtf.bogus" in x["reversedName"])

    def test_create_table(self):
        o = XmlResult().parse(
            """<?xml version="1.0"?>

<resultset statement="show create table requestedNames" xmlns:xsi="">
  <row>
    <field name="Table">treat</field>
    <field name="Create Table">CREATE TABLE `treat` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`),
) ENGINE=InnoDB AUTO_INCREMENT=10101 DEFAULT CHARSET=utf8
 PARTITION BY RANGE (`id`)
(PARTITION `p_start` VALUES LESS THAN MAXVALUE ENGINE = InnoDB)</field>
  </row>
</resultset>"""
        )

        self.assertEqual(len(o), 1)
        for x in o:
            self.assertEqual(x["Table"], "treat")
            self.assertEqual(
                x["Create Table"],
                """CREATE TABLE `treat` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`),
) ENGINE=InnoDB AUTO_INCREMENT=10101 DEFAULT CHARSET=utf8
 PARTITION BY RANGE (`id`)
(PARTITION `p_start` VALUES LESS THAN MAXVALUE ENGINE = InnoDB)""",
            )

    def test_truncated_resultset(self):
        with self.assertRaises(TruncatedDatabaseResultException):
            XmlResult().parse(
                """<?xml version="1.0"?>

<resultset statement="select * from here limit 1" xmlns:xsi="">
  <row>
    <field name="id">1</field>
  </row>"""
            )

        with self.assertRaises(TruncatedDatabaseResultException):
            XmlResult().parse(
                """<?xml version="1.0"?>

<resultset statement="select * from here limit 1" xmlns:xsi="">
  <row>
    <field name="id">1</field>"""
            )
