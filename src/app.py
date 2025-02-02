from typing import Dict
import json
import hashlib

from flask import Flask, abort, render_template, session, request
from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    RegistrationCredential,
    AuthenticationCredential,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

from .models import Credential, UserAccount

# Create our Flask app
app = Flask(__name__)
app.secret_key="My secret key"

################
#
# RP Configuration
#
################

rp_id = "localhost"
origin = "http://localhost:5000"
#rp_id = "ekblad.org"
#origin = "https://ekblad.org:5000"
rp_name = "Sample RP"
user_id = "some_random_user_identifier_like_a_uuid"
username = f"your.name@{rp_id}"
print(f"User ID: {user_id}")
print(f"Username: {username}")

# A simple way to persist credentials by user ID
in_memory_db: Dict[str, UserAccount] = {}

# Register our sample user
#in_memory_db[user_id] = UserAccount(
#    id=user_id,
#    username=username,
#    credentials=[],
#)

# Passwordless assumes you're able to identify the user before performing registration or
# authentication
logged_in_user_id = user_id

# A simple way to persist challenges until response verification
current_registration_challenge = None
current_authentication_challenge = None


################
#
# Views
#
################


@app.route("/")
def index():
    global username
    context = {
        "username": username,
    }
    return render_template("index.html", **context)

@app.route("/logout")
def logout():
    global username
    user = session.get('logged_in_user')
    if user:
        print("User "+user+" logged out!")
        del session['logged_in_user']
    context = {
        "username": username,
    }
    return render_template("index.html", **context)

@app.route("/secret")
def secret():
    global username

    user = session.get('logged_in_user')
    if user:
        print("User "+user+" logged in render secret page")
        return render_template("secret.html", username=user)
    else:
        print("You are not logged in!")
        context = {
            "username": username,
        }
        return render_template("index.html", **context)

################
#
# Registration
#
################


@app.route("/generate-registration-options", methods=["GET"])
def handler_generate_registration_options():
    global current_registration_challenge
    global logged_in_user_id

    entered_username=request.args.get("username")
    if not ('@' in entered_username):
        entered_username = f"{entered_username}@{rp_id}"
    m = hashlib.sha256()
    m.update(b"some string")
    m.update(entered_username.encode())
    entered_user_id=m.hexdigest()[0:32]
    if entered_user_id in in_memory_db:
        print("User "+entered_username+" already exists")
        abort(406)

    in_memory_db[entered_user_id] = UserAccount(
        id=entered_user_id,
        username=entered_username,
        credentials=[],
    )

    user = in_memory_db[entered_user_id]

    print("adding user:"+user.username+"("+user.id+")")
    logged_in_user_id = entered_user_id
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user.id,
        user_name=user.username,
        exclude_credentials=[
            {"id": cred.id, "transports": cred.transports, "type": "public-key"}
            for cred in user.credentials
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )

    current_registration_challenge = options.challenge

    return options_to_json(options)


@app.route("/verify-registration-response", methods=["POST"])
def handler_verify_registration_response():
    global current_registration_challenge
    global logged_in_user_id

    body = request.get_data()

    try:
        credential = RegistrationCredential.parse_raw(body)
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=current_registration_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except Exception as err:
        print("Got error "+str(err))
        return {"verified": False, "msg": str(err), "status": 400}

    user = in_memory_db[logged_in_user_id]

    new_credential = Credential(
        id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=json.loads(body).get("transports", []),
    )

    user.credentials.append(new_credential)

    print("Credentials for user "+user.username+" :")
    for credential in user.credentials:
       print("credential_id: "+str(credential.id))
       print("public_key   : "+str(credential.public_key))
       print("sign_count   : "+str(credential.sign_count))

    return {"verified": True}


################
#
# Authentication
#
################


@app.route("/generate-authentication-options", methods=["GET"])
def handler_generate_authentication_options():
    global current_authentication_challenge
    global logged_in_user_id

    entered_username=request.args.get("username")
    if not ('@' in entered_username):
        entered_username = f"{entered_username}@{rp_id}"
    m = hashlib.sha256()
    m.update(b"some string")
    m.update(entered_username.encode())
    entered_user_id=m.hexdigest()[0:32]

    print("Looking up user "+entered_username+"("+entered_user_id+")")

    if entered_user_id in in_memory_db:
        print("- found!")
        user = in_memory_db[entered_user_id]
        logged_in_user_id = entered_user_id
    else: 
        print("- NOT found!")
        abort(404)


    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            {"type": "public-key", "id": cred.id, "transports": cred.transports}
            for cred in user.credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    current_authentication_challenge = options.challenge

    return options_to_json(options)


@app.route("/verify-authentication-response", methods=["POST"])
def hander_verify_authentication_response():
    global current_authentication_challenge
    global logged_in_user_id

    body = request.get_data()

    try:
        credential = AuthenticationCredential.parse_raw(body)

        # Find the user's corresponding public key
        user = in_memory_db[logged_in_user_id]
        user_credential = None
        for _cred in user.credentials:
            if _cred.id == credential.raw_id:
                user_credential = _cred

        if user_credential is None:
            raise Exception("Could not find corresponding public key in DB")

        # Verify the assertion
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=current_authentication_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=user_credential.public_key,
            credential_current_sign_count=user_credential.sign_count,
            require_user_verification=False,
        )
    except Exception as err:
        return {"verified": False, "msg": str(err), "status": 400}

    # Update our credential's sign count to what the authenticator says it is now
    user_credential.sign_count = verification.new_sign_count

    print("Credentials for user "+user.username+" :")
    for credential in user.credentials:
       print("credential_id: "+str(credential.id))
       print("public_key   : "+str(credential.public_key))
       print("sign_count   : "+str(credential.sign_count))

    session['logged_in_user'] = user.username;

    return {"verified": True}
