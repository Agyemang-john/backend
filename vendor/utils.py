from vendor.models import Vendor


def get_vendor(request):
    """Returns vendor"""
    vendor = Vendor.objects.get(user=request.user)
    return vendor

def send_sms(phone_number, message):
    url = "https://sms.mhiskall.tech/sendsms/"
    apikey = '74APBFK2ZEYKHF8'
    sender_id = 'ADEPAMALL'
    urll = 'https://sms.mhiskall.tech/sendsms?apikey={}&from={}&to={}&message={}'.format(apikey,sender_id,phone_number,message)

    # headers = {
    #     'Authorization': f"Bearer {apikey}",  # Add a space after "Bearer"
    #     'Content-Type': 'application/json'
    # }
    # payload = json.dumps({
    #     'sender': sender_id,
    #     'to': phone_number,
    #     "message": message
    # })
    
    response = requests.request('GET',urll)
    return response.json()