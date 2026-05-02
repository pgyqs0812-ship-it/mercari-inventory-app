import os
import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

def create_ticket(summary, description):
    url = f"{JIRA_URL}/rest/api/3/issue"

    auth = (JIRA_EMAIL, JIRA_API_TOKEN)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    payload = {
        "fields": {
            "project": {
                "key": PROJECT_KEY
            },
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": description
                            }
                        ]
                    }
                ]
            },
            "issuetype": {
                "name": "Task"
            }
        }
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        auth=auth
    )

    if response.status_code == 201:
        issue_key = response.json()["key"]
        print(f"Ticket 创建成功: {issue_key}")
    else:
        print("创建失败:")
        print(response.text)


if __name__ == "__main__":
    summary = "测试：Mercari库存App Jira连接"
    description = "这是第一条自动创建的Jira测试Ticket"

    create_ticket(summary, description)