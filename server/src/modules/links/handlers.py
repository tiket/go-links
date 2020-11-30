import base64
import datetime
from functools import wraps
import logging
from urllib.parse import urlencode

from flask import Blueprint, request, redirect, jsonify, abort, g
from flask_login import current_user, login_required
import jwt

from modules.links import helpers
from modules.data import get_models
from modules.users import helpers as user_helpers
from shared_helpers import config
from shared_helpers.encoding import convert_entity_to_dict
from shared_helpers.events import enqueue_event


routes = Blueprint('links', __name__,
                   template_folder='../../static/templates')


models = get_models('links')


PUBLIC_KEYS = ['id', 'created', 'modified', 'owner', 'shortpath', 'destination_url', 'visits_count']


def get_field_conversion_fns():
  return {
    'visits_count': (lambda count: count or 0),
    'created': (lambda created: str(created).split('+')[0]),
    'modified': (lambda created: str(created).split('+')[0])
  }


def link_mutation_permission_required(f):
  @wraps(f)
  def decorated_view(*args, **kwargs):
    link = check_mutate_authorization(kwargs['link_id'])
    if not link:
      abort(403)

    g.link = link

    return f(*args, **kwargs)

  return decorated_view


def check_mutate_authorization(link_id, user_id=None):
  if user_id:
    user = user_helpers.get_user_by_id(user_id)
  else:
    user = current_user
  try:
    existing_link = models.ShortLink.get_by_id(link_id)
  except Exception as e:
    logging.warning(str(e))

    return False

  if not existing_link:
    return False

  if (existing_link.owner != user.email
      and not (user.organization == existing_link.organization
               and user_helpers.is_user_admin(user))):
    return False

  return existing_link


def _get_link_response(link):
  return convert_entity_to_dict(link, PUBLIC_KEYS, get_field_conversion_fns())


@routes.route('/_/api/links', methods=['GET'])
@login_required
def get_links():
  links = [convert_entity_to_dict(entity, PUBLIC_KEYS, get_field_conversion_fns())
           for entity in helpers.get_all_shortlinks_for_org(current_user.organization)]

  for link in links:
    link['mine'] = link['owner'] == current_user.email

  return jsonify(links)


@routes.route('/_/api/links', methods=['POST'])
@login_required
def post_link():
  object_data = request.json

  if 'owner' in object_data and not user_helpers.is_user_admin(current_user):
    abort(403)

  try:
    new_link = helpers.create_short_link(current_user.organization,
                                         object_data.get('owner', current_user.email),
                                         object_data['shortpath'],
                                         object_data['destination'])
  except helpers.LinkCreationException as e:
    return jsonify({
      'error': str(e)
    })

  logging.info(f'{current_user.email} created go link with ID {new_link.id}')

  return jsonify(
    convert_entity_to_dict(new_link, PUBLIC_KEYS, get_field_conversion_fns())
  ), 201


@routes.route('/_/api/links/<link_id>', methods=['PUT'])
@link_mutation_permission_required
@login_required
def put(link_id):
  existing_link = g.link

  object_data = request.json

  existing_link.destination_url = object_data['destination']

  try:
    return jsonify(_get_link_response(helpers.update_short_link(existing_link)))
  except helpers.LinkCreationException as e:
    return jsonify({
      'error': str(e),
      'error_type': 'error_bar'
    })


@routes.route('/_/api/links/<link_id>', methods=['DELETE'])
@link_mutation_permission_required
@login_required
def delete(link_id):
  existing_link = g.link

  logging.info('Deleting link: %s' % (convert_entity_to_dict(existing_link, PUBLIC_KEYS)))

  existing_link.delete()

  enqueue_event('link.deleted',
                'link',
                convert_entity_to_dict(existing_link, PUBLIC_KEYS, get_field_conversion_fns()))

  return jsonify({})


@routes.route('/_/api/links/<link_id>/transfer_link', methods=['POST'])
@link_mutation_permission_required
@login_required
def create_transfer_link(link_id):
  TOKEN_DURATION_IN_HOURS = 24

  payload = {'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_DURATION_IN_HOURS),
             'sub': f'link:{link_id}',
             'tp': 'transfer',  # "tp" -> token permissions
             'o': user_helpers.get_or_create_user(g.link.owner, g.link.organization).id,  # "o" -> link owner
             'by': current_user.id}

  token = jwt.encode(payload, config.get_config()['sessions_secret'], algorithm='HS256')

  full_url = f"{request.host_url}_transfer/{base64.urlsafe_b64encode(token).decode('utf-8').strip('=')}"

  return jsonify({'url': full_url}), 201


class InvalidTransferToken(Exception):
  pass


@routes.route('/_/api/transfer_link/<transfer_link_token>', methods=['POST'])
@login_required
def use_transfer_link(transfer_link_token):
  user_facing_error = None

  try:
    padded_token = transfer_link_token + '=' * (4 - len(transfer_link_token) % 4)  # add any missing base64 padding

    payload = jwt.decode(base64.urlsafe_b64decode(padded_token),
                         config.get_config()['sessions_secret'],
                         'HS256')
  except (jwt.exceptions.ExpiredSignatureError,
          jwt.exceptions.InvalidSignatureError,
          jwt.exceptions.DecodeError) as e:
    if type(e) is jwt.exceptions.ExpiredSignatureError:
      user_facing_error = 'Your transfer link has expired'

      logging.info('Attempt to use expired token: %s', transfer_link_token)
    if type(e) is jwt.exceptions.InvalidSignatureError:
      logging.warning('Attempt to use invalid token: %s', transfer_link_token)

    abort(403, user_facing_error or 'Your transfer link is no longer valid')

  try:
    if not payload['sub'].startswith('link:'):
      raise InvalidTransferToken('Subject is not link')

    if 'transfer' != payload['tp']:
      raise InvalidTransferToken('Invalid token permission')

    link_id = int(payload['sub'][len('link:'):])
    link = models.ShortLink.get_by_id(link_id)
    if not link:
      raise InvalidTransferToken('Link does not exist')

    owner_from_token = user_helpers.get_user_by_id(payload['o'])
    if not owner_from_token or link.owner != owner_from_token.email:
      user_facing_error = f'The owner of go/{link.shortpath} has changed since your transfer link was created'

      raise InvalidTransferToken('Owner from token does not match current owner')

    if not check_mutate_authorization(link_id, payload['by']):
      user_facing_error = f'The user who created your transfer link no longer has edit rights for go/{link.shortpath}'

      raise InvalidTransferToken('Token from unauthorized user')

    if current_user.organization != link.organization:
      raise InvalidTransferToken("Current user does not match link's organization")
  except (InvalidTransferToken,
          KeyError) as e:
    logging.warning(e)
    logging.warning('Attempt to use invalid token: %s', transfer_link_token)

    abort(403, user_facing_error or 'Your transfer link is no longer valid')

  link.owner = current_user.email
  link.put()

  return '', 201


@routes.route('/_transfer/<transfer_link_token>')
def redirect_transfer_url(transfer_link_token):
  if not current_user.is_authenticated:
    return redirect(f"/_/auth/login?{urlencode({'redirect_to': request.full_path})}")

  return redirect(f"/?{urlencode({'transfer': transfer_link_token})}")
