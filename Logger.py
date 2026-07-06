import datetime
import io
import json
import os
import sys
import time

import ConsoleUtils
from RingBuffer import RingBuffer


class ConsoleOutput(object):
    def __init__(self):
        self._status = ""

    def status(self, msg, current_time=""):
        status = str(msg)
        cols = ConsoleUtils.get_terminal_size()[0]
        if msg != "" and len(status) > cols:
            status = str(msg)[: cols - 4] + "..."
        update = "\r"
        update += status
        update += " " * max(0, len(self._status) - len(status))
        update += "\b" * max(0, len(self._status) - len(status))
        sys.stderr.write(update)
        sys.stderr.flush()
        self._status = status

    def printline(self, line):
        update = "\r"
        update += line + " " * max(0, len(self._status) - len(line)) + "\n"
        update += self._status
        sys.stderr.write(update)
        sys.stderr.flush()


class JsonOutput(object):
    def __init__(self, file, log_limit):
        self.jsonOutputFile = file
        self.jsonOutput = {}
        self.jsonOutputLog = RingBuffer(log_limit)
        self.clearStatusValues()

    def status(self, status, current_time):
        self.jsonOutput["last_update"] = current_time
        self.jsonOutput["last_status"] = status

    def printline(self, line):
        self.jsonOutputLog.append(line)

    def writeJsonFile(self):
        directory = os.path.dirname(self.jsonOutputFile)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with io.open(self.jsonOutputFile, "w", encoding="utf-8") as f:
            self.jsonOutput["log"] = self.jsonOutputLog.get()
            f.write(json.dumps(self.jsonOutput, ensure_ascii=False, sort_keys=True))

    def statusValue(self, coin, key, value):
        if coin not in self.jsonOutputCoins:
            self.jsonOutputCoins[coin] = {}
        self.jsonOutputCoins[coin][key] = str(value)

    def clearStatusValues(self):
        self.jsonOutputCoins = {}
        self.jsonOutput["raw_data"] = self.jsonOutputCoins
        self.jsonOutputCurrency = {}
        self.jsonOutput["outputCurrency"] = self.jsonOutputCurrency

    def outputCurrency(self, key, value):
        self.jsonOutputCurrency[key] = str(value)

    def metaValue(self, key, value):
        self.jsonOutput[key] = value


class Logger(object):
    def __init__(self, jsonFile="", jsonLogSize=-1):
        self._lended = ""
        if jsonFile != "" and jsonLogSize != -1:
            self.console = JsonOutput(jsonFile, int(jsonLogSize))
        else:
            self.console = ConsoleOutput()
        self.refreshStatus()

    def timestamp(self):
        ts = time.time()
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def log(self, msg):
        self.console.printline(self.timestamp() + " " + str(msg))
        self.refreshStatus()

    def offer(self, amt, cur, rate, days, msg):
        line = (
            self.timestamp()
            + " 挂出 "
            + str(amt)
            + " "
            + str(cur)
            + "，日利率 "
            + str(float(rate) * 100)
            + "%，周期 "
            + str(days)
            + " 天，"
            + self.digestApiMsg(msg)
        )
        self.console.printline(line)
        self.refreshStatus()

    def cancelOrders(self, cur, msg):
        line = (
            self.timestamp()
            + " 处理 "
            + str(cur)
            + " 挂单，"
            + self.digestApiMsg(msg)
        )
        self.console.printline(line)
        self.refreshStatus()

    def refreshStatus(self, lended=""):
        if lended != "":
            self._lended = lended
        self.console.status(self._lended, self.timestamp())

    def updateStatusValue(self, coin, key, value):
        if hasattr(self.console, "statusValue"):
            self.console.statusValue(coin, key, value)

    def updateOutputCurrency(self, key, value):
        if hasattr(self.console, "outputCurrency"):
            self.console.outputCurrency(key, value)

    def updateMetaValue(self, key, value):
        if hasattr(self.console, "metaValue"):
            self.console.metaValue(key, value)

    def persistStatus(self):
        if hasattr(self.console, "writeJsonFile"):
            self.console.writeJsonFile()
        if hasattr(self.console, "clearStatusValues"):
            self.console.clearStatusValues()

    def digestApiMsg(self, msg):
        if msg is None:
            return ""
        if isinstance(msg, dict):
            return self.translateMessage(str(msg.get("message") or msg.get("error") or msg))
        if isinstance(msg, list):
            if len(msg) > 7:
                return self.translateMessage(str(msg[6]) + ": " + str(msg[7]))
            return self.translateMessage(str(msg))
        return self.translateMessage(str(msg))

    def translateMessage(self, message):
        if message == "dry-run":
            return "模拟运行"
        if message.startswith("dry-run, would cancel "):
            return "模拟运行，将取消 " + message.removeprefix("dry-run, would cancel ")
        if message.startswith("dry-run, would reprice "):
            return "模拟运行，将重定价 " + message.removeprefix("dry-run, would reprice ")
        return message
