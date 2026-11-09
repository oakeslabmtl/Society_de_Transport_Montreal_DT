from pyhocon import ConfigFactory
from config.resources import resource_file_path
import logging.config


def load_config(config_file_name) -> dict:
    file_path = resource_file_path(config_file_name)
    config = ConfigFactory.parse_file(file_path)
    return config


def config_logger(logger_conf_file):
    logging.config.fileConfig(resource_file_path(logger_conf_file))
