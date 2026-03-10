import requests
import time


def check_ip_and_connectivity():
    # 1. 检测当前的出口 IP 和地理位置
    print("正在检测出口 IP 信息...")
    try:
        # 使用 ipapi.co 获取详细信息
        ip_resp = requests.get('https://ipapi.co/json/', timeout=10, verify=False)
        if ip_resp.status_code == 200:
            data = ip_resp.json()
            print(f"当前 IP: {data.get('ip')}")
            print(f"地理位置: {data.get('city')}, {data.get('region')}, {data.get('country_name')}")
            print(f"服务商: {data.get('org')}")
        else:
            print("无法获取详细 IP 信息。")
    except Exception as e:
        print(f"获取 IP 信息失败: {e}")

    print("-" * 30)

    # 2. 检测与 Anthropic (Claude) 官方接口的连通性
    print("正在测试 Anthropic API 连通性...")
    target_url = "https://api.anthropic.com/_healthcheck"
    try:
        start_time = time.time()
        # 注意：如果你在本地有环境变量代理，requests 默认会读取
        # 如果你想手动指定代理，可以使用 proxies={'http': '...', 'https': '...'}
        response = requests.get(target_url, timeout=15, verify=False)
        duration = (time.time() - start_time) * 1000

        if response.status_code == 200:
            print(f"✅ 成功连接到 Anthropic! 响应时间: {duration:.2f}ms")
        else:
            print(f"⚠️ 连接成功但状态码异常: {response.status_code}")
    except Exception as e:
        print(f"❌ 无法连接到 Anthropic 官方接口。")
        print(f"错误原因: {e}")


if __name__ == "__main__":
    check_ip_and_connectivity()