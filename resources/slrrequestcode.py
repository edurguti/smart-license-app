# Copyright (c) 2019 Cisco and/or its affiliates.
#
# This software is licensed to you under the terms of the Cisco Sample
# Code License, Version 1.1 (the "License"). You may obtain a copy of the
# License at
#
#                https://developer.cisco.com/docs/licenses
#
# All use of the material herein must be in accordance with the terms of
# the License. All rights not expressly granted by the License are
# reserved. Unless required by applicable law or agreed to separately in
# writing, software distributed under the License is distributed on an "AS
# IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.
import sqlite3

from flask_restful import Resource
from netmiko import ConnectHandler
import time
import threading
from models.slr import slr
from models.tokens import TokensModel
from flask_jwt import jwt_required
from models.sl_logger import SlLogger
from models.helper import Helper
from collections import defaultdict
import config
import json


# Constant for SLR request code table name
SLR_REQUEST_CODE_TABLE_NAME = "slr_request_code_tbl"
# List of strings for request token command nad request UDI command
req_token_command = ["license smart reservation", "end", "license smart reservation request local"]
req_udi_command = ["end", "sh license tech support | i Entitlement|Count"]
dlc_conversion_command = "show license data conversion | i conversion_data"
dlc_proc_stat_cmd = "show platform software license dlc | i DLC Process Status"
dlc_conversion_stat_cmd = "show platform software license dlc | i DLC Conversion Status"

logger = SlLogger.get_logger(__name__)

dlc_conversion_api_body = defaultdict(list)

class SlrRequestCode(Resource):

    def __init__(self):
        self.slr = slr("", "", "")
        pass

    def __del__(self):
        del self.slr
        pass

    @jwt_required()
    def get(self, uuid):
        threads = []
        logger.info(uuid)
        try:
            rows = TokensModel.find_by_uuid(uuid, "device_store")
        except Exception as e:
            print(e)
            logger.error({"message": "Data search operation failed!"}, exc_info=True)
            return {"message": "Data search operation failed!"}, 500

        for row in rows:
            logger.info("Launching threads to get auth tokens")
            # Updating the response status Step 2 started
            response_update = {'status': "S2s"}
            TokensModel.update(uuid, response_update, "upload_info_store")
            self.slr.update_status(SLR_REQUEST_CODE_TABLE_NAME, row[0], row[1], "Started", "step1")
            th = threading.Thread(target=SlrRequestCode.execute_cli_wrapper,
                                  args=(row[1], row[2], row[3], req_token_command, row[0], row[4], row[5], row[6]))
            th.start()
            threads.append(th)

        logger.info({"request": "accepted"})
        return {"request": "accepted"}, 201

    @classmethod
    def execute_cli_wrapper(cls, device_ip, username, password, cli, uuid, sa, va, domain):
        logger.info("Value of username is " + username + " password " + password)
        config.ERROR, sw_ver_str, device_type_dict = Helper.check_dlc_required(device_ip, uuid, sa, va, domain, "x",
                                                                               username, password)
        dlc_required = TokensModel.select_dlc(uuid)
        logger.info("DLC_Required Flag set to:" + dlc_required)
        if not config.ERROR:
            if sw_ver_str and device_type_dict['device_type'] is not None:
                s = slr("", "", "")
                s.update_req_token(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, "")
                result = ""
                result_lic_count = ""
                try:
                    if dlc_required == "True":
                        dlc_process_status = SlrRequestCode.check_dlc_status_on_device(device_ip, username, password,
                                                                                       dlc_proc_stat_cmd)
                        if dlc_process_status != '' and dlc_process_status == 'Not Complete':
                            dlc_conversion_data = SlrRequestCode.execute_dlc_cli(device_ip, username, password,
                                                                                         dlc_conversion_command)
                            if dlc_conversion_data != '':
                                logger.info("DLC_conversion_data")
                                logger.info(type(dlc_conversion_data))
                                logger.info(dlc_conversion_data)
                                SlrRequestCode.generate_dlc_data_dict(device_ip, dlc_conversion_data, va)
                                SlrRequestCode.insert_dlc_data_to_table(uuid, device_ip, dlc_conversion_data)
                    lic_rows = s.find_by_uuid_ipaddr(uuid, SLR_REQUEST_CODE_TABLE_NAME, device_ip)
                    device_license = s.get_license(lic_rows[0])
                    if device_license is None:
                        output = SlrRequestCode.config_commands(device_ip, username, password, cli)
                        logger.info("Value of the output is " + output)
                        req_code = output.split('\n')[-2]
                        if req_code.find("Request code:") != -1:
                            data = req_code.split("code: ")
                            req_code = data[1]
                        s.update_req_token(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, req_code)
                        output = SlrRequestCode.config_commands(device_ip, username, password, req_udi_command)
                        logger.info(output)
                        udi = (output.split('\n'))
                        logger.info("Value of udi is " + str(udi))
                        first = 1
                        first_count = 1
                        for entitlement_tag in udi:
                            logger.info(entitlement_tag)
                            if "Entitlement tag" in entitlement_tag:
                                if first:
                                    lic = entitlement_tag.split(":")[-1].replace(" ", "")
                                    first = 0
                                else:
                                    lic = entitlement_tag.split(":")[-1]
                                result = result + lic
                            if "Count:" in entitlement_tag:
                                if first_count:
                                    lic_count_string = entitlement_tag.split(":")[-1].replace(" ", "")
                                    first_count = 0
                                else:
                                    lic_count_string = entitlement_tag.split(":")[-1]
                                result_lic_count = result_lic_count + lic_count_string
                        logger.info("Value of entitlement tag is " + result)
                        logger.info("Value of count of licenses is " + result_lic_count)
                        # If we don't get lic ent tag and count from the device, indicate error
                        if (result == "") or (result_lic_count == ""):
                            result = "LIC_ENT_TAG_NOT_FOUND"
                            result_lic_count = "LIC_COUNT_NOT_FOUND"
                        s.update_entitlement_tag(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, result)
                        s.update_license_count(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, result_lic_count)
                    else:
                        output = SlrRequestCode.config_commands(device_ip, username, password, cli)
                        logger.info("Value of the output is " + output)
                        req_code = output.split('\n')[-2]
                        if req_code.find("Request code:") != -1:
                            data = req_code.split("code: ")
                            req_code = data[1]
                        s.update_req_token(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, req_code)
                        s.update_entitlement_tag(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, device_license)
                    # If we don't get lic ent tag and count from the device, it is considered as failed
                    if (result == "LIC_ENT_TAG_NOT_FOUND") or (result_lic_count == "LIC_COUNT_NOT_FOUND"):
                        s.update_status(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip,
                                        "License details not found from the "
                                        "device", "step1")
                    else:
                        s.update_status(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, "Completed", "step1")
                except Exception as e:
                    print(e)
                    s.update_status(SLR_REQUEST_CODE_TABLE_NAME, uuid, device_ip, str(e).split(":")[0], "step1")
                    # Added 04/16/19 - As export button and get auth key button is enabled eventhough
                    # connection to device timed-out
                    # Updating response status to Step 2 failed
                    response_update = {'status': "S2f"}
                    TokensModel.update(uuid, response_update, "upload_info_store")

                rows = s.find_by_step_status(SLR_REQUEST_CODE_TABLE_NAME, uuid, "Started", "step1")
                rows_completed = s.find_by_step_status(SLR_REQUEST_CODE_TABLE_NAME, uuid, "Completed", "step1")
                if (len(rows) == 0) and (len(rows_completed) != 0):
                    # Updating the response status to Step 2 completed
                    response_update = {'status': "S2c"}
                    TokensModel.update(uuid, response_update, "upload_info_store")
                del s
            else:
                logger.info("==>> Unsupported Network Device type...")
                response = {
                    'ipaddr': device_ip,
                    'username': username,
                    'password': password,
                    'sa_name': sa,
                    'va_name': va,
                    'domain': domain,
                    'status': 'Unsupported Device PID!'
                }
                config.ERROR = True
                TokensModel.update(uuid, response, "device_status_store")

        else:
            logger.error("No connectivity to the device...")
            response_update = {
                'ipaddr': device_ip,
                'username': username,
                'password': password,
                'sa_name': sa,
                'va_name': va,
                'domain': domain,
                'status': 'No Connectivity!'
            }
            config.ERROR = True
            TokensModel.update(uuid, response_update, "device_status_store")

    @classmethod
    def config_commands(cls, device_ip, username, password, command_list):
        device = {
            'device_type': 'cisco_xe',
            'ip': device_ip,
            'username': username,
            'password': password,
            "global_delay_factor": 0.1,
        }

        # Give some time for Registration operation to be complete
        time.sleep(1)

        net_connect = ConnectHandler(**device)
        device_prompt = net_connect.find_prompt()

        logger.info(" Starting CLI configuration process on device: {}".format(device_prompt))
        if device_prompt:
            # start_time = time.time()
            output = net_connect.send_config_set(config_commands=command_list, delay_factor=0.1)
            # print("Time taken for SLR step {}".format(time.time() - start_time))
        else:
            logger.info("Not able to get device prompt for ip address: {}".format(device_ip))

        net_connect.disconnect()
        return output

    @classmethod
    def check_dlc_status_on_device(cls, device_ip, username, password, dlc_proc_stat_cmd):
        device = {
            'device_type': 'cisco_xe',
            'ip': device_ip,
            'username': username,
            'password': password,
            "global_delay_factor": 0.1,
        }
        dlc_process_status = ""
        logger.info("DLC Process Status Cli:")
        logger.info(dlc_proc_stat_cmd)
        try:
            net_connect = ConnectHandler(**device)
            device_prompt = net_connect.find_prompt()

            logger.info("Starting DLC CLI configuration process on device: {}".format(device_prompt))
            if device_prompt:
                dlc_process_status = net_connect.send_command(dlc_proc_stat_cmd)
            net_connect.disconnect()
        except Exception as e:
            print(e)
        if dlc_process_status == '':
            return ''
        return dlc_process_status.split(':')[1].strip()

    @classmethod
    def execute_dlc_cli(cls, device_ip, username, password, dlc_cli):
        device = {
            'device_type': 'cisco_xe',
            'ip': device_ip,
            'username': username,
            'password': password,
            "global_delay_factor": 0.1,
        }
        logger.info("DLC Cli:")
        logger.info(dlc_cli)
        try:
            net_connect = ConnectHandler(**device)
            device_prompt = net_connect.find_prompt()

            logger.info("Starting DLC CLI configuration process on device: {}".format(device_prompt))
            if device_prompt:
                dlc_output = net_connect.send_command(dlc_cli)
                logger.info("DLC cli output")
                logger.info(type(dlc_output))
                logger.info(dlc_output)
                if dlc_output == '{"conversion_data":[]}':
                    dlc_conversion_data = ''
                else:
                    dlc_conversion_data = dlc_output.split("conversion_data")
                    dlc_conversion_data = dlc_conversion_data[1]
                    dlc_conversion_data = dlc_conversion_data[3:-1]
                    logger.info("DLC conversion data:")
                    '# converting dlc conversion data to dictionary'
                    dlc_conversion_data = json.loads(dlc_conversion_data)
                    logger.info(dlc_conversion_data)
                    logger.info(type(dlc_conversion_data))
            else:
                logger.info("Not able to get device prompt for ip address: {}".format(device_ip))

            net_connect.disconnect()
        except Exception as e:
            logger.error("Connection to the device Failed")
            logger.error(e)
        return dlc_conversion_data

    @classmethod
    def generate_dlc_data_dict(cls, device_ip, dlc_conversion_data, va_name):
        dlc_data_dict = dict()
        sudi_replace_keys = {'udi_pid': 'udiPid', 'udi_serial_number': 'udiSerialNumber'}
        sudi_dict = dict((sudi_replace_keys[key], value) for (key, value) in dlc_conversion_data['sudi'].items())
        dlc_data_dict.update({'sudi': sudi_dict})
        dlc_data_dict['sudi'].update({'uuid': config.UUID})
        dlc_data_dict['sudi'].update({'device_ip': device_ip})
        dlc_data_dict.update({'softwareTagIdentifier': dlc_conversion_data['software_tag_identifier']})
        dlc_data_dict.update({'conversionLines': dlc_conversion_data['conversion_lines']})
        conversion_lines_replace_keys = {'conversion_type':'conversionType',
                                         'conversion_encoding_type':'conversionEncodingType',
                                         'conversion_string':'conversionString', 'conversion_count':'conversionCount'}
        conversion_lines_array = list()
        for i in dlc_data_dict['conversionLines']:
            conversion_lines_array.append(
                dict((conversion_lines_replace_keys[key], value) for (key, value) in i.items()))
        dlc_data_dict.update({'conversionLines': conversion_lines_array})
        logger.info("Code after fix for dlc_data dict:")
        logger.info(dlc_data_dict)
        global dlc_conversion_api_body
        dlc_conversion_api_body[va_name].append(dlc_data_dict)

    @classmethod
    def insert_dlc_data_to_table(cls, uuid, device_ip, dlc_conversion_data):
        slr_type = "slr"
        udi_pid = dlc_conversion_data['sudi']['udi_pid']
        udi_serial_number = dlc_conversion_data['sudi']['udi_serial_number']
        software_tag_identifier = dlc_conversion_data['software_tag_identifier']
        conversion_lines = dlc_conversion_data['conversion_lines']

        connection = sqlite3.connect('data.db')
        cursor = connection.cursor()
        query = "INSERT INTO dlc_store VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        logger.info("Starting DLC data insertion into a table")

        for line in conversion_lines:
            cursor.execute(query, (uuid, device_ip, udi_pid, udi_serial_number, software_tag_identifier,
                                   line['conversion_type'], line['conversion_encoding_type'], line['conversion_string'],
                                   line['conversion_count'], slr_type))
            logger.info("Conversion Line:")
            logger.info(line)
        logger.info("DLC data insertion successful")
        connection.commit()
        connection.close()

    @classmethod
    def get_dlc_conversion_api_body(cls, uuid):
        logger.info("DLC API Body:")
        logger.info(dlc_conversion_api_body)
        connection = sqlite3.connect('data.db')
        cursor = connection.cursor()
        query = "SELECT domain from device_store WHERE uuid=?"
        result = cursor.execute(query, (uuid,))
        domain_name = result.fetchone()[0]
        connection.commit()
        connection.close()
        return domain_name, dlc_conversion_api_body
