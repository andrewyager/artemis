import psycopg2
import radix
from utils import get_logger, exception_handler, RABBITMQ_HOST
from utils.service import Service
from kombu import Connection, Queue, Exchange, uuid, Consumer, Producer
from kombu.mixins import ConsumerProducerMixin
import time
import pickle
import json
import logging
import hashlib

log = logging.getLogger('artemis_logger')

class Postgresql_db(Service):


    def run_worker(self):
        try:
            with Connection(RABBITMQ_HOST) as connection:
                self.worker = self.Worker(connection)
                self.worker.run()
        except:
            log.exception('exception')
        finally:
            log.info('stopped')


    class Worker(ConsumerProducerMixin):

        def __init__(self, connection):
            self.connection = connection
            self.prefix_tree = None
            self.rules = None
            self.timestamp = -1
            self.unhadled_to_feed_to_detection = 50
            self.insert_bgp_entries = []
            self.update_bgp_entries = []
            self.handled_bgp_entries = []
            self.tmp_hijacks_dict = dict()

            # DB variables
            self.db_conn = None
            self.db_cur = None
            self.create_connect_db()

            # EXCHANGES
            self.config_exchange = Exchange('config', channel=connection, type='direct', durable=False, delivery_mode=1)
            self.update_exchange = Exchange('bgp-update', channel=connection, type='direct', durable=False, delivery_mode=1)
            self.update_exchange.declare()

            self.hijack_exchange = Exchange('hijack-update', type='direct', durable=False, delivery_mode=1)
            self.handled_exchange = Exchange('handled-update', type='direct', durable=False, delivery_mode=1)
            self.db_clock_exchange = Exchange('db-clock', type='direct', durable=False, delivery_mode=1)
            self.mitigation_exchange = Exchange('mitigation', type='direct', durable=False, delivery_mode=1)


            # QUEUES
            self.update_queue = Queue('db-bgp-update', exchange=self.update_exchange, routing_key='update', durable=False, exclusive=True, max_priority=1,
                    consumer_arguments={'x-priority': 1})
            self.hijack_queue = Queue('db-hijack-update', exchange=self.hijack_exchange, routing_key='update', durable=False, exclusive=True, max_priority=1,
                    consumer_arguments={'x-priority': 1})
            self.hijack_update = Queue('db-hijack-fetch', exchange=self.hijack_exchange, routing_key='fetch-hijacks', durable=False, exclusive=True, max_priority=1,
                    consumer_arguments={'x-priority': 2})
            self.hijack_resolved_queue = Queue('db-hijack-resolve', exchange=self.hijack_exchange, routing_key='resolved', durable=False, exclusive=True, max_priority=2,
                    consumer_arguments={'x-priority': 2})
            self.handled_queue = Queue('db-handled-update', exchange=self.handled_exchange, routing_key='update', durable=False, exclusive=True, max_priority=1,
                    consumer_arguments={'x-priority': 1})
            self.config_queue = Queue('db-config-notify', exchange=self.config_exchange, routing_key='notify', durable=False, exclusive=True, max_priority=2,
                    consumer_arguments={'x-priority': 2})
            self.db_clock_queue = Queue('db-db-clock', exchange=self.db_clock_exchange, routing_key='db-clock-message', durable=False, exclusive=True, max_priority=2,
                    consumer_arguments={'x-priority': 3})
            self.mitigate_queue = Queue('db-mitigation-start', exchange=self.mitigation_exchange, routing_key='mit-start', durable=False, exclusive=True, max_priority=2,
                    consumer_arguments={'x-priority': 2})


            self.config_request_rpc()
            log.info('started')


        def get_consumers(self, Consumer, channel):
            return [
                    Consumer(
                        queues=[self.config_queue],
                        on_message=self.handle_config_notify,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.update_queue],
                        on_message=self.handle_bgp_update,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.hijack_queue],
                        on_message=self.handle_hijack,
                        prefetch_count=1,
                        no_ack=True,
                        accept=['pickle']
                        ),
                    Consumer(
                        queues=[self.db_clock_queue],
                        on_message=self._scheduler_instruction,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.handled_queue],
                        on_message=self.handle_handled_bgp_update,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.hijack_update],
                        on_message=self.handle_hijack_update,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.hijack_resolved_queue],
                        on_message=self.handle_resolved_hijack,
                        prefetch_count=1,
                        no_ack=True
                        ),
                    Consumer(
                        queues=[self.mitigate_queue],
                        on_message=self.handle_mitigation_request,
                        prefetch_count=1,
                        no_ack=True
                        )
                    ]

        def config_request_rpc(self):
            self.correlation_id = uuid()
            callback_queue = Queue(uuid(), durable=False, max_priority=2,
                    consumer_arguments={'x-priority': 2})

            self.producer.publish(
                '',
                exchange = '',
                routing_key = 'config-request-queue',
                reply_to = callback_queue.name,
                correlation_id = self.correlation_id,
                retry = True,
                declare = [callback_queue, Queue('config-request-queue', durable=False, max_priority=2)],
                priority = 2
            )
            with Consumer(self.connection,
                        on_message=self.handle_config_request_reply,
                        queues=[callback_queue],
                        no_ack=True):
                while self.rules is None:
                    self.connection.drain_events()

        def handle_bgp_update(self, message):
            # log.debug('message: {}\npayload: {}'.format(message, message.payload))
            msg_ = message.payload
            # prefix, key, origin_as, peer_as, as_path, service, type, communities, timestamp, hijack_id, handled, matched_prefix
            extract_msg = (msg_['prefix'], msg_['key'], str(msg_['path'][-1]), str(msg_['peer_asn']), msg_['path'], msg_['service'], \
                msg_['type'], json.dumps([(k['asn'],k['value']) for k in msg_['communities']]), float(msg_['timestamp']), 0, 'false', self.find_best_prefix_match(msg_['prefix']), msg_['orig_path'] )
            self.insert_bgp_entries.append(extract_msg)

        def handle_hijack(self, message):
            # log.debug('message: {}\npayload: {}'.format(message, message.payload))
            msg_ = message.payload
            key = msg_['key']
            if key not in self.tmp_hijacks_dict:
                self.tmp_hijacks_dict[key] = {}
                self.tmp_hijacks_dict[key]['prefix'] = msg_['prefix']
                self.tmp_hijacks_dict[key]['hijacker'] = str(msg_['hijacker'])
                self.tmp_hijacks_dict[key]['hij_type'] = str(msg_['hij_type'])
                self.tmp_hijacks_dict[key]['time_started'] = int(msg_['time_started'])
                self.tmp_hijacks_dict[key]['time_last'] = int(msg_['time_last'])
                self.tmp_hijacks_dict[key]['peers_seen'] = msg_['peers_seen']
                self.tmp_hijacks_dict[key]['inf_asns'] = msg_['inf_asns']
                self.tmp_hijacks_dict[key]['monitor_keys'] = msg_['monitor_keys']
                self.tmp_hijacks_dict[key]['time_detected'] = int(msg_['time_detected'])
            else:
                self.tmp_hijacks_dict[key]['time_started'] = int(min(self.tmp_hijacks_dict[key]['time_started'], msg_['time_started']))
                self.tmp_hijacks_dict[key]['time_last'] = int(max(self.tmp_hijacks_dict[key]['time_last'], msg_['time_last']))
                self.tmp_hijacks_dict[key]['peers_seen'].update(msg_['peers_seen'])
                self.tmp_hijacks_dict[key]['inf_asns'].update(msg_['inf_asns'])
                self.tmp_hijacks_dict[key]['monitor_keys'].update(msg_['monitor_keys'])

        def handle_handled_bgp_update(self, message):
            # log.debug('message: {}\npayload: {}'.format(message, message.payload))
            key_ = (message.payload,)
            self.handled_bgp_entries.append(key_)


        def build_radix_tree(self):
            self.prefix_tree = radix.Radix()
            for rule in self.rules:
                for prefix in rule['prefixes']:
                    node = self.prefix_tree.search_exact(prefix)
                    if node is None:
                        node = self.prefix_tree.add(prefix)
                        node.data['confs'] = []

                    conf_obj = {'origin_asns': rule['origin_asns'], 'neighbors': rule['neighbors']}
                    node.data['confs'].append(conf_obj)

        def find_best_prefix_match(self, prefix):
            prefix_node = self.prefix_tree.search_best(prefix)
            if prefix_node is not None:
                return prefix_node.prefix
            else:
                return ''

        def handle_config_notify(self, message):
            log.debug('Message: {}\npayload: {}'.format(message, message.payload))
            config = message.payload
            if config['timestamp'] > self.timestamp:
                self.timestamp = config['timestamp']
                self.rules = config.get('rules', [])
                self.build_radix_tree()
                self._save_config(config)

        def handle_config_request_reply(self, message):
            log.debug('Message: {}\npayload: {}'.format(message, message.payload))
            if self.correlation_id == message.properties['correlation_id']:
                config = message.payload
                if config['timestamp'] > self.timestamp:
                    self.timestamp = config['timestamp']
                    self.rules = config.get('rules', [])
                    self.build_radix_tree()
                    self._save_config(config)

        def handle_hijack_update(self, message):
            results = []
            self.db_cur.execute("SELECT time_started, time_last, num_peers_seen, num_asns_inf, key  FROM hijacks WHERE active = true;")
            entries = self.db_cur.fetchall()
            for entry in entries:
                results.append({ 'time_started' : entry[0], 'time_last' : entry[1], 'peers_seen' : entry[2], 'inf_asns' : entry[3], 'key': entry[4]})
            self.producer.publish(
                results,
                exchange = self.hijack_exchange,
                routing_key = 'fetch-hijacks',
                retry = True,
                priority = 1
            )

        def handle_resolved_hijack(self, message):
            raw = message.payload
            try:
                self.db_cur.execute("UPDATE hijacks SET active=false, time_ended=" + str(int(time.time())) + " WHERE key='" + str(raw) + "';" )
                self.db_conn.commit()
            except Exception:
                log.exception('exception')

        def handle_mitigation_request(self, message):
            raw = message.payload
            try:
                self.db_cur.execute("UPDATE hijacks SET time_started=" + str(int(raw['time'])) + " WHERE key=" + str(raw['key']) + ";" )
                self.db_conn.commit()
            except Exception:
                log.exception('exception')

        def create_tables(self):
            bgp_updates_table = "CREATE TABLE IF NOT EXISTS bgp_updates ( " + \
                "id INTEGER GENERATED ALWAYS AS IDENTITY, " + \
                "key VARCHAR ( 32 ) NOT NULL PRIMARY KEY, " + \
                "prefix inet, " + \
                "origin_as INTEGER, " + \
                "peer_asn   INTEGER, " + \
                "as_path   text[], " + \
                "service   VARCHAR ( 50 ), " + \
                "type  VARCHAR ( 1 ), " + \
                "communities  json, " + \
                "timestamp BIGINT, " + \
                "hijack_key VARCHAR ( 32 ), " + \
                "handled   BOOLEAN, " + \
                "matched_prefix inet, " + \
                "orig_path json )"

            bgp_hijacks_table = "CREATE TABLE IF NOT EXISTS hijacks ( " + \
                "id   INTEGER GENERATED ALWAYS AS IDENTITY, " + \
                "key VARCHAR ( 32 ) NOT NULL PRIMARY KEY, " + \
                "type  VARCHAR ( 1 ), " + \
                "prefix    inet, " + \
                "hijack_as VARCHAR ( 6 ), " + \
                "num_peers_seen   INTEGER, " + \
                "num_asns_inf INTEGER, " + \
                "time_started BIGINT, " + \
                "time_last BIGINT, " + \
                "time_ended   BIGINT, " + \
                "mitigation_started   BIGINT, " + \
                "time_detected BIGINT," + \
                "under_mitigation BOOLEAN, " + \
                "resolved  BOOLEAN, " + \
                "active  BOOLEAN, " + \
                "ignored BOOLEAN ) "
                

            configs_table = "CREATE TABLE IF NOT EXISTS configs ( " + \
                "id   INTEGER GENERATED ALWAYS AS IDENTITY, " + \
                "key VARCHAR ( 32 ) NOT NULL PRIMARY KEY, " + \
                "config_data  json, " + \
                "time_modified BIGINT)"

            self.db_cur.execute(bgp_updates_table)
            self.db_cur.execute(bgp_hijacks_table)
            self.db_cur.execute(configs_table)
            self.db_conn.commit()

        def create_connect_db(self):
            try:
                connect_str = "dbname='artemis_db' user='artemis_user' host='postgres' " + \
                    "password='Art3m1s'"
                self.db_conn = psycopg2.connect(connect_str)
                self.db_cur = self.db_conn.cursor()
                self.create_tables()
            except:
                log.exception('exception')
            finally:
                log.debug('PostgreSQL DB created/connected..')


        def _insert_bgp_updates(self):
            try:
                self.db_cur.executemany("INSERT INTO bgp_updates (prefix, key, origin_as, peer_asn, as_path, service, type, communities, " + \
                    "timestamp, hijack_key, handled, matched_prefix, orig_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;", self.insert_bgp_entries)
                self.db_conn.commit()
            except:
                log.exception('exception')
                self.db_conn.rollback()
                return -1
            finally:
                num_of_entries = len(self.insert_bgp_entries)
                self.insert_bgp_entries.clear()
                return num_of_entries

        def _update_bgp_updates(self):
            num_of_updates = 0
            # Update the BGP entries using the hijack messages
            for hijack_key in self.tmp_hijacks_dict:
                for bgp_entry_to_update in self.tmp_hijacks_dict[hijack_key]['monitor_keys']:
                    num_of_updates += 1
                    self.update_bgp_entries.append((str(hijack_key), bgp_entry_to_update))

            if len(self.update_bgp_entries) > 0:
                try:
                    self.db_cur.executemany("UPDATE bgp_updates SET handled=true, hijack_key=%s WHERE key=%s ", self.update_bgp_entries)
                    self.db_conn.commit()
                except:
                    log.exception('exception')
                    self.db_conn.rollback()
                    return -1

            # Update the BGP entries using the handled messages
            if len(self.handled_bgp_entries) > 0:
                try:
                    self.db_cur.executemany("UPDATE bgp_updates SET handled=true WHERE key=%s", self.handled_bgp_entries)
                    self.db_conn.commit()
                except:
                    log.exception('handled bgp entries {}'.format(self.handled_bgp_entries))
                    self.db_conn.rollback()
                    return -1

            num_of_updates += len(self.handled_bgp_entries)
            self.handled_bgp_entries.clear()
            return num_of_updates


        def _insert_update_hijacks(self):
            for key in self.tmp_hijacks_dict:
                try:
                    cmd_ = "INSERT INTO hijacks (key, type, prefix, hijack_as, num_peers_seen, num_asns_inf, "
                    cmd_ += "time_started, time_last, time_ended, mitigation_started, time_detected, under_mitigation, active, resolved, ignored) VALUES ("
                    cmd_ += "'" + str(key) + "','" + self.tmp_hijacks_dict[key]['hij_type'] + "','" + self.tmp_hijacks_dict[key]['prefix'] + "','" + self.tmp_hijacks_dict[key]['hijacker'] + "'," 
                    cmd_ += str(len(self.tmp_hijacks_dict[key]['peers_seen'])) + "," + str(len(self.tmp_hijacks_dict[key]['inf_asns'])) + "," + str(self.tmp_hijacks_dict[key]['time_started']) + "," 
                    cmd_ += str(int(self.tmp_hijacks_dict[key]['time_last'])) + ", 0, 0, " + str(self.tmp_hijacks_dict[key]['time_detected']) + ", false, true, false, false )"
                    cmd_ += "ON CONFLICT(key) DO UPDATE SET num_peers_seen=" + str(len(self.tmp_hijacks_dict[key]['peers_seen'])) + ", num_asns_inf=" + str(len(self.tmp_hijacks_dict[key]['inf_asns']))
                    cmd_ += ", time_started=" + str(int(self.tmp_hijacks_dict[key]['time_started'])) + ", time_last=" + str(int(self.tmp_hijacks_dict[key]['time_last']))

                    self.db_cur.execute(cmd_)
                    self.db_conn.commit()
                except:
                    log.exception('exception')
                    self.db_conn.rollback()
                    return -1

            num_of_entries = len(self.tmp_hijacks_dict)
            self.tmp_hijacks_dict.clear()
            return num_of_entries

        def _retrieve_unhandled(self):
            results = []
            self.db_cur.execute("SELECT * FROM bgp_updates WHERE handled = false ORDER BY id DESC LIMIT(" + str(self.unhadled_to_feed_to_detection) + ");")
            entries = self.db_cur.fetchall()
            for entry in entries:
                results.append({ 'key' : entry[1], 'prefix' : entry[2], 'origin_as' : entry[3], 'peer_asn' : entry[4], 'path': entry[5], \
                  'service' : entry[6], 'type' : entry[7], 'communities' : entry[8], 'timestamp' : int(entry[9])})
            if len(results):
                self.producer.publish(
                    results,
                    exchange = self.update_exchange,
                    routing_key = 'unhandled',
                    retry = False,
                    priority = 2
                )

        def _update_bulk(self):
            inserts, updates, hijacks = self._insert_bgp_updates(), self._update_bgp_updates(), self._insert_update_hijacks()
            str_ = ""
            if inserts > 0:
                str_ += "BGP Updates Inserted: {}\n".format(inserts)
            if updates > 0:
                str_ += "BGP Updates Updated: {}\n".format(updates)
            if hijacks > 0:
                str_ += "Hijacks Inserted: {}".format(hijacks)
            if str_ != "":
                log.debug('{}'.format(str_))

        def _scheduler_instruction(self, message):
            msg_ = message.payload
            if (msg_ == 'bulk_operation'):
                self._update_bulk()
                return
            elif(msg_ == 'send_unhandled'):
                self._retrieve_unhandled()
                return
            else:
                log.warning('Received uknown instruction from scheduler: {}'.format(msg_))

        def _save_config(self, config):
            try:
                log.debug("Config Store..")
                config_hash = hashlib.md5(pickle.dumps(config)).hexdigest()
                self.db_cur.execute("INSERT INTO configs (key, config_data, time_modified) VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING;", (config_hash, json.dumps(config), int(time.time())))
                self.db_conn.commit()
            except:
                log.exception("failed to save config in db")





