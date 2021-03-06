#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
(C) Copyright 2020 Hewlett Packard Enterprise Development LP.

Licensed under the Apache License, Version 2.0 (the "License"); you may
not use this file except in compliance with the License. You may obtain
a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
"""

# import from OS
import subprocess
import time
import os
import sys
import configparser
from random import *
from multiprocessing import Process, Lock
import socket
import inspect
import threading
import concurrent.futures
import signal
import base64
import json
import random

# import special classes
import uuid
from confluent_kafka import Producer as KafkaProducer
from confluent_kafka import Consumer as KafkaConsumer, KafkaError, KafkaException
from optparse import OptionParser
from schema_registry.client import SchemaRegistryClient
from schema_registry.serializers import MessageSerializer

# project imports
from version import __version__
import KrakenMareLogger
from agentcommon import AgentCommon


### START IBswitchSimulator class ##################################################
class FanIn(AgentCommon):
    registered = False
    loggerName = None
    # All time is in seconds as float. We use time_ns to get highest resolution
    timet0 = 0
    t0_on_first_mqtt = 0
    MsgCount = 0

    def __init__(
        self, configFile, debug, encrypt, TopicForThisProcess=False, batching=False, mqttcounter=False, mqttPassthrough=False
    ):
        """
                Class init
        """

        self.sensors = []
        self.mqttTopicList = []

        self.loggerName = "simulator.agent." + __version__ + ".log"

        self.config = self.checkConfigurationFile(
            configFile, ["Daemon", "Logger", "Kafka", "MQTT"]
        )

        self.kafka_broker = self.config.get("Kafka", "kafka_broker")
        self.kafka_port = int(self.config.get("Kafka", "kafka_port"))
        self.kafkaProducerTopic = self.config.get("Kafka", "kafkaProducerTopic")
        self.kafka_security_protocol = self.config.get("Kafka", "kafka_security.protocol")
        self.kafka_sasl_mechanisms = self.config.get("Kafka", "kafka_sasl.mechanisms")
        self.kafka_sasl_username = self.config.get("Kafka", "kafka_sasl.username")
        self.kafka_sasl_password = self.config.get("Kafka", "kafka_sasl.password")
        self.kafka_ssl_ca_location = self.config.get("Kafka", "kafka_ssl.ca.location")
        self.kafka_ssl_certificate_location = self.config.get("Kafka", "kafka_ssl.certificate.location")
        self.kafka_ssl_key_location = self.config.get("Kafka", "kafka_ssl.key.location")
        self.kafka_ssl_key_password = self.config.get("Kafka", "kafka_ssl.key.password")
        self.myFanIn_mqtt_encryption_enabled = encrypt
        self.mqtt_broker = self.config.get("MQTT", "mqtt_broker")
        self.mqtt_port = int(self.config.get("MQTT", "mqtt_port"))
        self.mqttBatching = batching
        self.enableMQTTbatchCount = mqttcounter
        self.enableMQTTpassthrough = mqttPassthrough

        # create topic list: [ ("topicName1", int(qos1)),("topicName2", int(qos2)) ]
        #                    [ ("ibswitch", 0), ("redfish", 0)]
        addValue = []
        if TopicForThisProcess != False:
            # multi-process version
            value = TopicForThisProcess.split(":")
            addValue.append(value[0])
            addValue.append(int(value[1]))
            self.processID = os.getpid()
            self.threadID = threading.get_ident()
            self.logMPMT = "P-{:d} | T-{:d} |".format(self.processID, self.threadID)
            
            if not self.enableMQTTpassthrough:
                print(
                    "MULTIPROC {:s} Starting FanIn Gateway in its process for {:s}. MQTT batch pass-through is DISABLED.".format(
                        self.logMPMT, TopicForThisProcess
                    )
                )
            else:
                print(
                    "MULTIPROC {:s} Starting FanIn Gateway in its process for {:s}. MQTT batch pass-through is ENABLED.".format(
                        self.logMPMT, TopicForThisProcess
                    )
                )
                
            self.mqttTopicList.append(addValue)
            self.processID = os.getpid()
            self.myFanInGatewayName = "FanIn-test[" + str(self.processID) + "]"
        else:
            # single threaded version
            value = self.config.get("MQTT", "mqttSingleThreadTopic").split(":")
            addValue.append(value[0])
            addValue.append(int(value[1]))
            self.mqttTopicList.append(addValue)
            self.myFanInGatewayName = "FanIn-test"

        addValue = []
        value = self.config.get("MQTT", "mqttRegistrayionResultTopic").split(":")
        addValue.append(value[0])
        addValue.append(int(value[1]))
        self.mqttTopicList.append(addValue)

        self.bootstrapServerStr = self.kafka_broker + ":" + str(self.kafka_port)

        # Register to the framework
        self.myFanInGateway_id = -1
        self.myFanInGateway_debug = debug
        self.myFanInGateway_uuid = str(uuid.uuid4())
        self.myFanInGateway_uid = self.myFanInGatewayName + str(
            random.randint(1, 100001)
        )

        # for thread safe counter
        self.myFanInGateway_threadLock = threading.Lock()

        self.myMQTTregistered = False
        self.kafka_producer = None
        self.kafka_consumer = None

        self.kafka_msg_counter = 1
        self.kafka_msg_ack_received = 0

        super().__init__(configFile, debug)

        #message counter per uuid
        self.myMQTTtopicCounterPerUUID = {}

    def resetLogLevel(self, logLevel):
        """
                Resets the log level 
        """
        self.logger = KrakenMareLogger().getLogger(self.loggerName, logLevel)

    #######################################################################################
    # MQTT agent methods
    # sends MQTT messages to Kafka (in batches)
    # TODO: do we need multiple threads here?
    # TODO: have processing method per client type OR topic for each sensor type to convert messages?
    def mqtt_on_message(self, client, userdata, message):
        if self.myFanInGateway_debug == True:
            print("mqtt_on_message start")

        query_data = []
        k = 0

        if message.topic == self.mqttTopicList[0][0]:
            self.done = False
            if self.timet0 == 0:
                self.timet0 = time.time_ns() / 1000000000

            if self.t0_on_first_mqtt == 0:
                self.t0_on_first_mqtt = time.time_ns() / 1000000000
            
            # if passthrough is enabled, send mqtt batch directly to kafka
            if not self.enableMQTTpassthrough:
                
                if self.mqttBatching == True:
                    query_data = self.msg_serializer.decode_message(message.payload)
                else:
                    query_data.append(message.payload)
    
                for data in query_data["tripletBatch"]:
                    # check, if I know agent UUID and adjust my MQTT topic counter accordingly
                    if (self.enableMQTTbatchCount == True):
                        try:
                            # if current batch count is -1 smaller then SEND count do nothing since this is ok
                            if not (self.myMQTTtopicCounterPerUUID[data["sensorUuid"]] == (int(data["sensorValue"]) - 1)) and not (self.myMQTTtopicCounterPerUUID[data["sensorUuid"]] - int(data["sensorValue"]) == 0):
                                print("ATTENTION: Missing # of MQTTbatches for agent UUID: " + str(data["sensorUuid"]) + " and topic: " + str(self.mqttTopicList[0][0]) + " is:" +  str(int(data["sensorValue"]) - self.myMQTTtopicCounterPerUUID[data["sensorUuid"]]))
                            
                            self.myMQTTtopicCounterPerUUID[data["sensorUuid"]] = int(data["sensorValue"])
                                                
                            if self.myFanInGateway_debug == True:
                                logMPMT = str("P-{:d} : ".format(os.getpid()))
                                print(logMPMT + self.mqttTopicList[0][0] + "| UUID: "+ str(data["sensorUuid"]) + "| MQTT batch count: " + str(self.myMQTTtopicCounterPerUUID[data["sensorUuid"]]))
                        except:
                            self.myMQTTtopicCounterPerUUID[data["sensorUuid"]] = int(data["sensorValue"])
                            if int(data["sensorValue"]) != 1:
                                print("ATTENTION: Missing # of MQTTbatches for agent UUID: " + str(data["sensorUuid"]) + " and topic: " + str(self.mqttTopicList[0][0]) + " is: " +  str(int(data["sensorValue"])-1))
                        
                    try:
    #                    print(str(data["sensorUuid"]) + ", " + str(data["sensorValue"]))
                        raw_bytes = self.msg_serializer.encode_record_with_schema_id(
                            self.send_time_series_schema_id, data
                        )
                        self.kafka_producer.produce(
                            self.kafkaProducerTopic,
                            raw_bytes,
                            on_delivery=self.kafka_producer_on_delivery,
                        )
                        self.kafka_msg_counter += 1
                        k += 1
    
                        if self.myFanInGateway_debug == True:
                            print(str(self.kafka_msg_counter) + ":published to Kafka")
    
                        if self.kafka_msg_counter % 1000 == 0:
                            deltat = time.time_ns() / 1000000000 - self.timet0
                            deltaMsg = self.kafka_msg_counter - self.MsgCount
                            self.MsgCount = self.kafka_msg_counter
                            self.timet0 = time.time_ns() / 1000000000
                            elapsed = (int)(
                                time.time_ns() / 1000000000 - self.t0_on_first_mqtt
                            )
                            logMPMT = "{:d} secs | Process-{:d} | Thread-{:d} | TopicMqtt-{:s}".format(
                                elapsed,
                                os.getpid(),
                                threading.get_ident(),
                                str(message.topic),
                            )
                            print(
                                logMPMT
                                + " | "
                                + str(self.kafka_msg_counter)
                                + " messages published to Kafka, rate = {:.2f} msg/sec".format(
                                    deltaMsg / deltat
                                )
                            )
    
                    except BufferError as e1:
                        print(
                            "%% Local producer queue is full (%d messages awaiting delivery): try again\n"
                            % len(self.kafka_producer)
                        )
                        print(e1)
                    except KafkaException as e2:
                        print("MQTT message not published to Kafka! Cause is ERROR:")
                        print(e2)
                        
                if not k == 47:
                    print("Samples in last processed message was: " + str(k))
                    
            else:
                #passthrough
                try:
                    # print(str(data["sensorUuid"]) + ", " + str(data["sensorValue"]))
                    self.kafka_producer.produce(
                        self.kafkaProducerTopic,
                        message.payload,
                        on_delivery=self.kafka_producer_on_delivery,
                    )
                    self.kafka_msg_counter += 1
                    
                    if self.myFanInGateway_debug == True:
                        print(str(self.kafka_msg_counter) + ":published to Kafka")
                        
                    if self.kafka_msg_counter % 1000 == 0:
                        deltat = time.time_ns() / 1000000000 - self.timet0
                        deltaMsg = self.kafka_msg_counter - self.MsgCount
                        self.MsgCount = self.kafka_msg_counter
                        self.timet0 = time.time_ns() / 1000000000
                        elapsed = (int)(
                            time.time_ns() / 1000000000 - self.t0_on_first_mqtt
                        )
                        logMPMT = "{:d} secs | Process-{:d} | Thread-{:d} | TopicMqtt-{:s}".format(
                            elapsed,
                            os.getpid(),
                            threading.get_ident(),
                            str(message.topic),
                        )
                        print(
                            logMPMT
                            + " | "
                            + str(self.kafka_msg_counter)
                            + " MQTT batch messages published to Kafka, rate = {:.2f} msg/sec".format(
                                deltaMsg / deltat
                            )
                        )

                except BufferError as e1:
                    print(
                        "%% Local producer queue is full (%d messages awaiting delivery): try again\n"
                        % len(self.kafka_producer)
                    )
                    print(e1)
                except KafkaException as e2:
                    print("MQTT message not published to Kafka! Cause is ERROR:")
                    print(e2)
                
            self.mqttMsgTimer = time.time()
            
        else:
            if self.myFanInGateway_debug == True:
                print("Not ibswitch topic")

    # END MQTT agent methods
    #######################################################################################

    #######################################################################################
    # Kafka agent methods

    # Kafka error printer
    def kafka_producer_error_cb(self, err):
        logMPMT = "P-{:d} | T-{:d} |".format(os.getpid(), threading.get_ident())
        print("{:s} KAFKA_PROD_CALLBACK_ERR : {:s}".format(logMPMT, str(err)))

    def kafka_producer_on_delivery(self, err, msg):
        if err:
            print(
                "KAFKA_MESSAGE_CALLBACK_ERR : %% Message failed delivery: %s - to %s [%s] @ %s\n"
                % (err, msg.topic(), str(msg.partition()), str(msg.offset()))
            )
        else:
            self.kafka_msg_ack_received += 1
            if self.myFanInGateway_debug == True:
                print(
                    "%% Message delivered to %s [%d] @ %d\n"
                    % (msg.topic(), msg.partition(), msg.offset())
                )

    # connect to Kafka broker as producer to check topic 'myTopic'
    def kafka_check_topic(self, myTopic):
        print("Connecting as kafka consumer to check for topic: " + myTopic)
        test = False

        conf = {
            "bootstrap.servers": self.bootstrapServerStr,
            "client.id": socket.gethostname(),
            "error_cb": self.kafka_producer_error_cb,
            "security.protocol": self.kafka_security_protocol,
            "sasl.mechanisms": self.kafka_sasl_mechanisms,
            "sasl.username": self.kafka_sasl_username,
            "sasl.password": self.kafka_sasl_password,
            "ssl.ca.location": self.kafka_ssl_ca_location,
            "ssl.certificate.location": self.kafka_ssl_certificate_location,
            "ssl.key.location": self.kafka_ssl_key_location,
            "ssl.key.password": self.kafka_ssl_key_password
        }

        while test == False:
            time.sleep(1)
            print("waiting for kafka producer to connect")

            try:
                # shouldn't be used directly: self.kafka_client = kafka.KafkaClient(self.kafka_broker)
                kafka_producer = KafkaProducer(conf)
                kafka_producer.list_topics(topic=myTopic, timeout=1)
                test = True
            except KafkaException as e:
                # print(e.args[0])
                print("waiting for " + myTopic + " topic...")

    # connect to Kafka broker as producer

    def kafka_producer_connect(self):
        test = False

        conf = {
            "bootstrap.servers": self.bootstrapServerStr,
            "client.id": socket.gethostname(),
            "error_cb": self.kafka_producer_error_cb,
            "security.protocol": self.kafka_security_protocol,
            "sasl.mechanisms": self.kafka_sasl_mechanisms,
            "sasl.username": self.kafka_sasl_username,
            "sasl.password": self.kafka_sasl_password,
            "ssl.ca.location": self.kafka_ssl_ca_location,
            "ssl.certificate.location": self.kafka_ssl_certificate_location,
            "ssl.key.location": self.kafka_ssl_key_location,
            "ssl.key.password": self.kafka_ssl_key_password,
            "linger.ms": 1000,
            "message.max.bytes": 2560000,
            "queue.buffering.max.messages": 2000000,
        }

        while test == False:
            time.sleep(2)
            print("waiting for kafka producer to connect")

            try:
                # shouldn't be used directly: self.kafka_client = kafka.KafkaClient(self.kafka_broker)
                self.kafka_producer = KafkaProducer(conf)
                self.kafka_producer.list_topics(timeout=1)
                test = True
            except KafkaException as e:
                print(e.args[0])
                print("waiting for Kafka brokers..." + self.bootstrapServerStr)

        print(
            self.__class__.__name__
            + "."
            + inspect.currentframe().f_code.co_name
            + ": producer connected"
        )

    # END Kafka agent methods
    #######################################################################################

    def signal_handler(self, signal, frame):
        self.mqtt_close()
        sys.exit(0)

    # main method of FanIn
    def run(self):
        # local and debug flag are not used from here at the moment

        # self.kafka_check_topic("registration-result")
        self.kafka_check_topic(self.kafkaProducerTopic)
        # self.mqtt_registration()
        self.kafka_producer_connect()
        # TODO: should be own process via process class (from multiprocessing import Process)
        # generate list of mqtt topics to subscribe, used in initial connection and to re-subscribe on re-connect

        mqttSubscriptionTopics = self.mqttTopicList
        print("MQTT topic list:" + str(self.mqttTopicList))

        # start mqtt client
        myLoopForever = False
        myCleanSession = True
        self.mqtt_init(
            self.myFanInGateway_uuid,
            mqttSubscriptionTopics,
            myLoopForever,
            myCleanSession,
            self.myFanIn_mqtt_encryption_enabled,
        )

        # start listening to data
        # self.mqtt_subscription()
        regularLog = 300
        logMPMT = str("P-{:d} : ".format(os.getpid()))
        self.done = False
        self.mqttMsgTimer = time.time()
        while True:
            time.sleep(0.05)
            self.kafka_producer.poll(0)
            regularLog -= 1
            if regularLog <= 0:
                regularLog = 300
            
            if self.mqttMsgTimer+10 < time.time() and self.done == False and self.enableMQTTbatchCount == True:
                for uuid in self.myMQTTtopicCounterPerUUID:
                    print(logMPMT + self.mqttTopicList[0][0] + "| UUID: "+ str(uuid) + "| MQTT batch count: " + str(self.myMQTTtopicCounterPerUUID[uuid]))
                    self.done = True
                
        self.mqtt_close
        print("FanIn terminated")


### END IBswitchSimulator class ##################################################


def main():
    topics_list = []

    usage = "usage: %s " % sys.argv[0]
    parser = OptionParser(usage=usage, version=__version__)

    parser.add_option(
        "--encrypt",
        action="store_true",
        default=False,
        dest="encrypt",
        help="specify this option in order to encrypt the mqtt connection",
    )
    parser.add_option(
        "--debug",
        action="store_true",
        default=False,
        dest="debug",
        help="specify this option in order to run in debug mode",
    )
    parser.add_option(
        "--batching",
        action="store_true",
        default=False,
        dest="batching",
        help="specify this option in order to enable MQTT batch processing",
    )
    parser.add_option(
        "--enableMQTTbatchesCounter",
        action="store_true",
        default=False,
        dest="checkmqtt",
        help="specify this option in order to enable the special processing of MQTT batches requiring special data from the simulator encoding a batch counter into the MQTT batch",
    )
    parser.add_option(
        "--enableMQTTbatchPassthrough",
        action="store_true",
        default=False,
        dest="mqttPassthrough",
        help="specify this option in order to pass MQTT batches without de-coding/re-encoding to the kafka producer.",
    )
    parser.add_option(
        "--numberOfTopic",
        dest="numberOfTopic",
        default=False,
        help="specify this option in order to publish to multiple topics (# of topics (need to be able to divide 16 by this,e.g. --numberOfTopic=2), defaults to 1",
    )
    parser.add_option(
        "--logLevel",
        dest="logLevel",
        help="specify the logger level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    (options, _) = parser.parse_args()

    option_dict = vars(options)

    if options.numberOfTopic:
        numberOfMqttTopics = int(option_dict["numberOfTopic"])
    else:
        numberOfMqttTopics = 1

    if numberOfMqttTopics > 1:
        print("MULTIPROC-MAIN - Starting")

        config = configparser.ConfigParser()
        config.read("FanIn.cfg")
        rootTopic = config.get("MQTT", "mqttMultiProcessRootTopic")
        rootTopicQOS = config.get("MQTT", "mqttMultiProcessTopicQOS")

        i = 0
        while i < numberOfMqttTopics:
            topics_list.append(rootTopic + "/" + str(i) + ":" + str(rootTopicQOS))
            i += 1

        def fanin_mp_launcher(
            debugP=False, encryptP=False, topic_to_listen="", batchingP=False, mqttcounterE=False, mqttPassthroughP=False
        ):
            myFanInMP = FanIn(
                "FanIn.cfg",
                debug=debugP,
                encrypt=encryptP,
                TopicForThisProcess=str(topic_to_listen),
                batching=batchingP,
                mqttcounter=mqttcounterE,
                mqttPassthrough=mqttPassthroughP,
            )
            signal.signal(signal.SIGINT, myFanInMP.signal_handler)
            myFanInMP.run()

        print("MULTIPROC-MAIN - List of topics = {:s}".format(str(topics_list)))
        for onetopic in topics_list:
            print(
                "MULTIPROC-MAIN - Forking FanIn Gateway process for {:s}".format(
                    onetopic
                )
            )
            NewP = Process(
                target=fanin_mp_launcher,
                kwargs={
                    "debugP": option_dict["debug"],
                    "encryptP": option_dict["encrypt"],
                    "topic_to_listen": onetopic,
                    "batchingP": option_dict["batching"],
                    "mqttcounterE": option_dict["checkmqtt"],
                    "mqttPassthroughP": option_dict["mqttPassthrough"],
                },
            )
            NewP.start()
            time.sleep(1)

        print(
            "MULTIPROC-MAIN - All processes launched, now main process will wait forever as Signals across Processes & threads is not handled."
        )
    else:
        myFanIn = FanIn(
            "FanIn.cfg",
            debug=option_dict["debug"],
            encrypt=option_dict["encrypt"],
            batching=option_dict["batching"],
            mqttcounter=option_dict["checkmqtt"],
            mqttPassthrough=option_dict["mqttPassthrough"],
        )
        signal.signal(signal.SIGINT, myFanIn.signal_handler)

        if options.logLevel:
            myFanIn.resetLogLevel(options.logLevel)

        myFanIn.run()


if __name__ == "__main__":
    main()
