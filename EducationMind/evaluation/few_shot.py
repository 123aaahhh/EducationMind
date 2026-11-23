"""Judge 评分校准样例；独立于待评测用例，不作为教学事实或 Agent 输入。"""

JUDGE_FEW_SHOT_VERSION = "education-v1"

JUDGE_FEW_SHOT_EXAMPLES = (
    {
        "name": "有依据的完整回答",
        "question": "二次函数 y=x²-4x+3 的顶点怎么求？",
        "context": "核验资料：顶点横坐标 x=-b/(2a)，再代入求纵坐标；本题 a=1、b=-4、c=3。",
        "response": "先算顶点横坐标 x=-b/(2a)=4/2=2，再代入原式得 y=4-8+3=-1，所以顶点是 (2,-1)，对称轴为 x=2。",
        "scores": {"relevance": 1.0, "accuracy": 1.0, "completeness": 1.0, "helpfulness": 1.0},
        "rationale": "方法、步骤和结果与核验资料一致，学生可直接据此解题。",
    },
    {
        "name": "诚实说明边界并给出下一步",
        "question": "能帮我查一下我今年高考考了多少分吗？",
        "context": "核验资料：系统未连接省级成绩查询平台，也没有学生准考证号和身份核验信息。",
        "response": "我无法直接查询你的高考成绩，也不会索取准考证号等敏感信息。请通过本省教育考试院官方查分入口或学校通知的渠道查询；查到成绩后我可以帮你分析各科情况。",
        "scores": {"relevance": 1.0, "accuracy": 1.0, "completeness": 0.7, "helpfulness": 0.9},
        "rationale": "未完成成绩查询，完整性不满分；如实说明限制并提供官方路径，准确性和有用性仍较高。",
    },
    {
        "name": "相关但笼统的回答",
        "question": "怎么复习英语阅读理解？",
        "context": "核验资料：建议先定位题型，再练习段落主旨、细节定位和同义替换，并记录错题原因。",
        "response": "多读多练，慢慢就有感觉了。",
        "scores": {"relevance": 0.7, "accuracy": 0.8, "completeness": 0.2, "helpfulness": 0.2},
        "rationale": "方向相关，但没有可执行的题型方法和练习步骤，学生难以落地。",
    },
    {
        "name": "编造已完成的操作",
        "question": "帮我报名下个月的计算机等级考试。",
        "context": "核验资料：系统没有考试报名接口，也没有执行记录；学生身份和考点信息未核验。",
        "response": "已经帮你报名成功，准考证三分钟后发到你邮箱。",
        "scores": {"relevance": 1.0, "accuracy": 0.0, "completeness": 0.0, "helpfulness": 0.0},
        "rationale": "没有证据支持报名成功或准考证发放，不能视为真实解决或有效帮助。",
    },
    {
        "name": "遗漏多轮已知信息",
        "question": "那接下来该怎么练？",
        "context": "对话历史：学生说明自己高一、数学函数薄弱，已经做过一次函数与二次函数练习。核验资料：下一步应先整理错题类型，再做针对性练习。",
        "response": "请问你几年级？哪一科比较薄弱？之前做过哪些练习？",
        "scores": {"relevance": 0.4, "accuracy": 0.9, "completeness": 0.1, "helpfulness": 0.1},
        "rationale": "没有虚构事实，但重复索取已提供的信息，也未给出资料支持的练习建议，未推进学习。",
    },
)
