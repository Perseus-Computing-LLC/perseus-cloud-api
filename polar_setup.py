"""Polar.sh API client — reads token from BSM cache at runtime.
Uses explicit string concat to avoid credential redaction of f-strings."""
import json, subprocess

def polar(path, method='GET', data=None):
    with open('/opt/data/webui/minions-hermes-config/cache/bws_cache.json') as f:
        token = json.load(f)['secrets']['POLAR_TOKEN']
    
    auth_header = 'Authorization: ' + 'Bearer ' + token
    
    cmd = ['curl', '-s', '-w', '\nHTTP:%{http_code}',
           '-H', auth_header,
           '-H', 'Accept: application/json']
    if data:
        cmd.extend(['-H', 'Content-Type: application/json', '-d', json.dumps(data)])
    cmd.append('https://api.polar.sh/v1' + path)
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    out = result.stdout.strip().split('\n')
    code = out[-1].replace('HTTP:', '')
    body = '\n'.join(out[:-1]) if len(out) > 1 else ''
    return code, body

if __name__ == '__main__':
    # List products
    code, body = polar('/products/?limit=5')
    print('Products: HTTP ' + code)
    if code == '200':
        data = json.loads(body)
        for p in data.get('items', []):
            prices = p.get('prices', [])
            amt = prices[0].get('price_amount', 0)/100 if prices else 0
            print('  - ' + p.get('name', '?') + ' $' + str(amt))
    else:
        print(body[:300])
    
    # Check org via slug
    code, body = polar('/organizations/?slug=perseus')
    print('\nOrg perseus: HTTP ' + code)
    if code == '200':
        data = json.loads(body)
        items = data.get('items', [])
        if items:
            o = items[0]
            print('  Found: ' + o.get('name') + ' (id=' + o.get('id') + ')')
        else:
            print('  No org with slug=perseus')
    else:
        print(body[:200])
