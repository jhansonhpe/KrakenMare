/**
 * (C) Copyright 2020 Hewlett Packard Enterprise Development LP.
 */
package com.hpe.krakenmare.impl;

import java.security.GeneralSecurityException;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;

import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.MockProducer;
import org.apache.kafka.clients.producer.Producer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import org.apache.kafka.common.serialization.StringSerializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.google.common.base.Strings;
import com.hpe.krakenmare.Main;

import io.confluent.kafka.serializers.AbstractKafkaAvroSerDeConfig;
import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import io.confluent.kafka.serializers.KafkaAvroDeserializerConfig;
import io.confluent.kafka.serializers.KafkaAvroSerializer;
import io.confluent.kafka.serializers.subject.RecordNameStrategy;

public class KafkaUtils {

	public final static Logger LOG = LoggerFactory.getLogger(KafkaUtils.class);

	public static final String BOOTSTRAP_SERVERS = Main.getProperty("bootstrap.servers");
	public static final String SCHEMA_REGISTRY = Main.getProperty("schema.registry");
	public static final String AGENT_REGISTRATION_TOPIC = Main.getProperty("km.agent-registration.kafka.topic");
	public static final String AGENT_DEREGISTRATION_TOPIC = Main.getProperty("km.agent-deregistration.kafka.topic");
	public static final String DEVICE_REGISTRATION_TOPIC = Main.getProperty("km.device-registration.kafka.topic");

	public static Producer<String, byte[]> createByteArrayProducer(String clientId) {
		if (Strings.isNullOrEmpty(BOOTSTRAP_SERVERS)) {
			LOG.warn("No Kafka boostrap servers configured, returning mock producer");
			return new MockProducer<>();
		}
		Properties props = new Properties();
		props.put(ProducerConfig.CLIENT_ID_CONFIG, clientId);
		props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, BOOTSTRAP_SERVERS);
		props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
		// we don't use KafkaAvroSerializer here as *we* do the serialization first, then actually send the bytes[] to Kafka
		// doing so, we can intercept the serialized bytes[] and send it to MQTT also
		// props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, KafkaAvroSerializer.class);
		props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class);

		props.putAll(getAvroConfig());

		return new KafkaProducer<>(props);
	}

	private static Map<String, Object> getAvroConfig() {
		if (SCHEMA_REGISTRY.startsWith("https://")) {
			try {
				MqttUtils.setUpTrustAllCerts();
			} catch (GeneralSecurityException e) {
				e.printStackTrace();
			}
		}

		Map<String, Object> map = new HashMap<>();
		map.put(AbstractKafkaAvroSerDeConfig.SCHEMA_REGISTRY_URL_CONFIG, SCHEMA_REGISTRY);
		// auto register schema during tests
		map.put(AbstractKafkaAvroSerDeConfig.AUTO_REGISTER_SCHEMAS, SCHEMA_REGISTRY.startsWith(/* AbstractKafkaAvroSerDe.MOCK_URL_PREFIX */ "mock://"));
		// https://github.com/confluentinc/schema-registry/issues/265
		map.put(KafkaAvroDeserializerConfig.SPECIFIC_AVRO_READER_CONFIG, true);
		map.put(AbstractKafkaAvroSerDeConfig.VALUE_SUBJECT_NAME_STRATEGY, RecordNameStrategy.class);
		return map;
	}

	public static KafkaAvroSerializer getAvroValueSerializer() {
		KafkaAvroSerializer ser = new KafkaAvroSerializer();
		ser.configure(getAvroConfig(), false);
		return ser;
	}

	public static KafkaAvroDeserializer getAvroValueDeserializer() {
		KafkaAvroDeserializer des = new KafkaAvroDeserializer();
		des.configure(getAvroConfig(), false);
		return des;
	}

}
