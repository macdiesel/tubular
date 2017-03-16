#! /usr/bin/env python3

"""
Command-line script to find the next release pipeline to advance
and then advance it by triggering the manual stage.
"""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

from os import path
import os
import io
import sys
import logging
import yaml
from dateutil import parser
import click


# Add top-level module path to sys.path before importing tubular code.
sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

from tubular.gocd_api import GoCDAPI  # pylint: disable=wrong-import-position
from tubular.hipchat import submit_hipchat_message  # pylint: disable=wrong-import-position

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
LOG = logging.getLogger(__name__)


@click.command()
@click.option(
    '--gocd_user',
    help=u"Username to use in logging into GoCD.",
    required=True,
)
@click.option(
    '--gocd_password',
    help=u"Password to use in logging into GoCD.",
    required=True,
)
@click.option(
    '--gocd_url',
    help=u"URL to use in logging into GoCD.",
    required=True,
)
@click.option(
    '--hipchat_token',
    help=u"HipChat token which authorizes message sending. (optional)",
)
@click.option(
    '--hipchat_channel',
    multiple=True,
    help=u"HipChat channel which to send the message. (optional)",
)
@click.option(
    '--pipeline',
    help=u"Name of the pipeline to advance.",
    required=True,
)
@click.option(
    '--stage',
    help=u"Name of the pipeline's stage to advance.",
    required=True,
)
@click.option(
    '--relative_dt',
    help=u"Datetime used when determining current release date in ISO 8601 format, YYYY-MM-DDTHH:MM:SS+HH:MM",
)
@click.option(
    '--out_file',
    help=u"File path to which to write pipeline advancement information (optional).",
)
def find_and_advance_pipeline(
        gocd_user, gocd_password, gocd_url, hipchat_token, hipchat_channel, pipeline, stage, relative_dt, out_file
):
    """
    Find the GoCD advancement pipeline that should be advanced/deployed to production - and advance it.
    """
    gocd = GoCDAPI(gocd_user, gocd_password, gocd_url)

    # If a datetime string was passed-in, convert it to a datetime.
    if relative_dt:
        relative_dt = parser.parse(relative_dt)

    pipeline_to_advance = gocd.fetch_pipeline_to_advance(pipeline, stage, relative_dt)
    gocd.approve_stage(
        pipeline_to_advance.name,
        pipeline_to_advance.counter,
        stage
    )
    advance_info = {
        'name': pipeline_to_advance.name,
        'counter': pipeline_to_advance.counter,
        'stage': stage,
        'url': pipeline_to_advance.url
    }
    LOG.info('Successfully advanced this pipeline: %s', advance_info)

    if out_file:
        directory = os.path.dirname(out_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with io.open(out_file, 'w') as artifact:
            yaml.safe_dump(advance_info, stream=artifact)

    if hipchat_token:
        submit_hipchat_message(
            hipchat_token,
            hipchat_channel,
            'PROD DEPLOY: Pipeline was advanced: {}'.format(pipeline_to_advance.url),
            "green"
        )


if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    find_and_advance_pipeline()
